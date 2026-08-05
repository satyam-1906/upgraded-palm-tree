import stat
from fastapi import FastAPI, HTTPException, Depends, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import os, boto3, hashlib
from datetime import datetime, timedelta
import copy
from sympy import det
from utils.otp_gen import otp_generator
from utils.send_email import email_send
from dotenv import load_dotenv
from schemas import InputSchema, ExtractSchema, CreateSchema, EmailSchema, LoginSchema
from database import Users, sessionLocal, SessionTokens
from botocore.config import Config
import jwt
import mimetypes
load_dotenv()
from utils.agent import lang_app, State
from utils.extractor import extract, extract_ocr, extract_csv, hash_text
import uuid
from redis import Redis
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from typing import Any, List
from utils.direct_ocr_extractor import ocr_extraction
from botocore.exceptions import ClientError
from schemas import VoucherSchema, BankStatementInputSchema, BRSInputSchema, GodownSchema, UnitSchema, StockSchema, NotificationLogSchema, InvoiceGenerationSchema, InvoiceSyncSchema, InvoiceSyncItemSchema
from database import Vouchers, BankStatements, BRS, godown, units, stock, notificationLogs
from utils.invoice_gen import generate_invoice, generate_voucher_pdf, clear


ph = PasswordHasher()
s3= boto3.client('s3', region_name=os.getenv("S3_REGION"), aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"), aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"), config=Config(signature_version="s3v4", s3={'addressing_style': 'virtual'}))
app=FastAPI()
redis_client=Redis(
    host=os.getenv("REDIS_HOST", 'comparison-hyperspeedy-canvas-69712.db.redis.io'),
    port=int(os.getenv("REDIS_PORT", 13818)),
    password=os.getenv("REDIS_PASSWORD"),
    decode_responses=True
)
binary_redis_client=Redis(
    host=os.getenv("REDIS_HOST", 'comparison-hyperspeedy-canvas-69712.db.redis.io'),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD"),
)
bucket=os.getenv("S3_BUCKET_NAME")

origins = ['http://localhost:5503', 'http://127.0.0.1:5501', 'http://127.0.0.1:5502', 'http://127.0.0.1', '0.0.0.0', 'http://localhost:3000']

app.add_middleware(CORSMiddleware,
                   allow_origins = origins,
                   allow_credentials = True,
                   allow_methods = ['*'],
                   allow_headers = ['*'])

def get_db():
    db=sessionLocal()
    try:
        yield db 
    finally:
        db.close()


@app.get("/")
def chek():
    return {"status": "Running"}

@app.post("/create")
def crea(payload: CreateSchema, db: Session=Depends(get_db)):
    email, username=payload.email, payload.username
    password=ph.hash(payload.password)
    db_note=Users(email=email, username=username, password=password, isactive=False)
    db.add(db_note)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"error: {str(e)}")
    db.refresh(db_note)
    otp= otp_generator()
    hashed= hashlib.sha256(otp.encode()).hexdigest()
    redis_client.setex(f"otp:{email}", 600, hashed)
    email_send(email, otp)
    return {"message": "OTP sent to your email"}

@app.post("/verify")
def veri(payload: EmailSchema, db:Session=Depends(get_db)):
    ot, email=payload.otp, payload.email
    key= f"otp:{email}"
    stored=redis_client.get(key)
    if not stored:
        raise HTTPException(status_code=404, detail="invalid otp or expired otp")
    input_hash=hashlib.sha256(ot.encode()).hexdigest()
    if input_hash != stored:
        raise HTTPException(status_code=401, detail="otp does not match")
    user = db.query(Users).filter(Users.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    user.isactive = True
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="database error")
    db.refresh(user)
    redis_client.delete(key)
    return {"message": "email verified. Proceed to login"}

@app.post("/login")
def logi(payload: LoginSchema, response: Response, db:Session=Depends(get_db)):
    username, password=payload.username, payload.password
    user = db.query(Users).filter(Users.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if user.isactive:
        passw = user.password
        try:
            ph.verify(passw, password)
            secret = os.getenv("SECRET")
            if not secret:
                raise HTTPException(status_code=500, detail="JWT secret not configured")
            pay = {
                "iss": "auth-service",
                "sub": username,
                "exp": datetime.utcnow() + timedelta(days=7)
            }
            token = jwt.encode(pay, secret, algorithm="HS256")
            response.set_cookie(key="session_token", value=token, httponly=True, secure=True, samesite="lax", max_age=604800)
            db_no = SessionTokens(username=username, token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=datetime.utcnow()+timedelta(days=7), revoked=False)
            db.add(db_no)
            try:
                db.commit()
            except Exception as e:
                db.rollback()
                raise HTTPException(status_code=500, detail="database error")
            db.refresh(db_no)
            return {"message": "login success"}
        except VerifyMismatchError:
            raise HTTPException(status_code=401, detail="passwords do not match")
    else:
        raise HTTPException(status_code=401, detail="verify your email")
           
@app.post("/upload")
def upl(payload: InputSchema):
    file_id=str(uuid.uuid4())
    key=f"docs/{file_id}-{payload.file_name}"
    pres=s3.generate_presigned_url(
        ClientMethod = 'put_object', 
        Params = {
            'Bucket': bucket, 
            "Key" : key, 
            "ContentType": payload.content_type
        }, 
        ExpiresIn = 600
    )
    status="uploaded"
    return {"upload_url": pres, "file_key": key, "status": status}

@app.post("/extract")
def extr(payload: ExtractSchema):
    combined_text = ""
    for idx, file_key in enumerate(payload.file_keys):
        file_ext = "pdf"
        if idx < len(payload.file_type) and payload.file_type[idx] is not None:
            file_ext = payload.file_type[idx].lower().strip(".")
        else:
            # Fallback extraction from file key name if payload.file_type is missing or incomplete
            file_ext = file_key.split(".")[-1].lower()

        response = s3.get_object(
            Bucket=bucket,
            Key=file_key
        )
        file_bytes = response["Body"].read()
        text = extract(file_bytes, file_ext)
        if len(text.strip()) < 100:
            text = extract_ocr(file_bytes, file_ext)
        combined_text += "\n\n" + text
    result = lang_app.invoke(State(content=combined_text))
    text_hash = hash_text(combined_text) #isko caching mein use karenge
    return {"csv_file": result["fin"], "normal": result["normal"]}



@app.post("/extract-OCR")
async def extractOCR(request: Request):
    content_type = request.headers.get("Content-Type")
    schema = request.headers.get("Schema")
    allowed_types = ["application/pdf", "image/jpeg", "image/png"]
    if content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Invalid file type")
    file_bytes = await request.body()
    binary_redis_client.set('cached_file', file_bytes, ex=600)
    redis_client.set('cached_file_type', content_type, ex=600)
    return ocr_extraction(file_bytes, content_type, schema)

@app.post("/upload-to-AWS")
async def upload_to_AWS(request: Request):
    content_type = redis_client.get('cached_file_type')
    schema = request.headers.get("Schema")
    allowed_types = ["application/pdf", "image/jpeg", "image/png"]
    ext = [".pdf", ".jpg", ".png"]
    if content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Invalid file type")
    file_bytes = binary_redis_client.get('cached_file')
    file_name = str(uuid.uuid4())
    redis_client.set("file_key", f'{file_name}{ext[allowed_types.index(content_type)]}', ex=600)
    prefix = "bank_statements" if schema == "bankStatement" else "vouchers"
    try:
        response = s3.put_object(
            Bucket=bucket,
            Key=f'{prefix}/{file_name}{ext[allowed_types.index(content_type)]}',
            Body=file_bytes,                  # Pass the byte array directly here
            ContentType=content_type
            )
        
        status_code = response['ResponseMetadata']['HTTPStatusCode']
    
        if status_code == 200:
            print("Upload successful!")
            print(f"File ETag (MD5 Hash): {response['ETag']}")
        else:
            print(f"Upload failed with status code: {status_code}")
    except ClientError as e:
        # Catches AWS-specific errors (Access Denied, Bucket Not Found, etc.)
        print(f"AWS Error: {e.response['Error']['Message']}")
    except Exception as e:
        # Catches network timeouts or system errors
        print(f"An unexpected error occurred: {e}")

@app.post("/add-voucher")
def add_voucher(payload: List[VoucherSchema], db: Session = Depends(get_db)):
    file_key = redis_client.get("file_key")
    vouchers_added = []
    for item in payload:
        voucher = Vouchers(
            voucher_type=item.voucher_type,
            date=item.date,
            voucher_no=item.voucher_no,
            party=item.party,
            items=item.items,
            amount=item.amount,
            gst_amount=item.gst_amount,
            discount=item.discount,
            status=item.status,
            file_key=file_key,
            meta_type=item.meta_type,
            meta=item.meta,
        )
        db.add(voucher)
        vouchers_added.append(voucher)
    
    try:
        db.commit()
        redis_client.delete("file_key")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
        
    for v in vouchers_added:
        db.refresh(v)
        
    return [
        {
            "id": v.id,
            "voucher_type": v.voucher_type,
            "date": v.date,
            "voucher_no": v.voucher_no,
            "party": v.party,
            "items": v.items,
            "amount": v.amount,
            "gst_amount": v.gst_amount,
            "discount": v.discount,
            "status": v.status,
            "meta_type": v.meta_type,
            "meta": v.meta,
        }
        for v in vouchers_added
    ]

@app.get("/vouchers")
def get_vouchers(db: Session = Depends(get_db)):
    rows = db.query(Vouchers).order_by(Vouchers.id.desc()).all()
    return [
        {
            "id": v.id,
            "voucher_type": v.voucher_type,
            "date": v.date,
            "voucher_no": v.voucher_no,
            "party": v.party,
            "items": v.items,
            "amount": v.amount,
            "gst_amount": v.gst_amount,
            "discount": v.discount,
            "status": v.status,
            "meta_type": v.meta_type,
            "meta": v.meta,
        }
        for v in rows
    ]

@app.put("/vouchers/{voucher_id}")
def update_voucher(voucher_id: int, payload: VoucherSchema, db: Session = Depends(get_db)):
    voucher = db.query(Vouchers).filter(Vouchers.id == voucher_id).first()
    if not voucher:
        raise HTTPException(status_code=404, detail="Voucher not found")
    voucher.voucher_type = payload.voucher_type
    voucher.date = payload.date
    voucher.voucher_no = payload.voucher_no
    voucher.party = payload.party
    voucher.items = payload.items
    voucher.amount = payload.amount
    voucher.gst_amount = payload.gst_amount
    voucher.discount = payload.discount
    voucher.status = payload.status
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(voucher)
    return {
        "id": voucher.id,
        "voucher_type": voucher.voucher_type,
        "date": voucher.date,
        "voucher_no": voucher.voucher_no,
        "party": voucher.party,
        "items": voucher.items,
        "amount": voucher.amount,
        "gst_amount": voucher.gst_amount,
        "discount": voucher.discount,
        "status": voucher.status
    }

@app.get("/vouchers/{voucher_id}")
def get_single_voucher(voucher_id: int, db: Session = Depends(get_db)):
    voucher = db.query(Vouchers).filter(Vouchers.id == voucher_id).first()
    if not voucher:
        raise HTTPException(status_code=404, detail="Voucher not found")
    return {
        "id": voucher.id,
        "voucher_type": voucher.voucher_type,
        "date": voucher.date,
        "voucher_no": voucher.voucher_no,
        "party": voucher.party,
        "items": voucher.items,
        "amount": voucher.amount,
        "gst_amount": voucher.gst_amount,
        "discount": voucher.discount,
        "status": voucher.status
    }

@app.delete("/vouchers/{voucher_id}")
def delete_voucher(voucher_id: int, db: Session = Depends(get_db)):
    voucher = db.query(Vouchers).filter(Vouchers.id == voucher_id).first()
    if not voucher:
        raise HTTPException(status_code=404, detail="Voucher not found")
    try:
        db.delete(voucher)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    return {"message": "Voucher deleted successfully"}

def _bs_to_dict(bs):
    return {
        "id": bs.id,
        "bank_name": bs.bank_name,
        "account_number": bs.account_number,
        "referrence_no": bs.referrence_no,
        "transaction_date": bs.transaction_date,
        "description": bs.description,
        "transaction_type": bs.transaction_type,
        "amount": bs.amount,
        "category": bs.category,
        "reconciliation_status": bs.reconciliation_status,
        "party_name": bs.party_name,
        "voucher_ref": bs.voucher_ref
    }

@app.post("/bank-statements")
def add_bank_statements(payload: List[BankStatementInputSchema], db: Session = Depends(get_db)):
    records_added = []
    for item in payload:
        bs = BankStatements(
            bank_name=item.bank_name,
            account_number=item.account_number,
            referrence_no=item.referrence_no,
            transaction_date=item.transaction_date,
            description=item.description,
            transaction_type=item.transaction_type,
            amount=item.amount,
            category=item.category,
            reconciliation_status=item.reconciliation_status,
            party_name=item.party_name,
            voucher_ref=item.voucher_ref
        )
        db.add(bs)
        records_added.append(bs)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    for r in records_added:
        db.refresh(r)
    return [_bs_to_dict(r) for r in records_added]

@app.get("/bank-statements")
def get_bank_statements(db: Session = Depends(get_db)):
    rows = db.query(BankStatements).order_by(BankStatements.id.desc()).all()
    return [_bs_to_dict(r) for r in rows]

@app.get("/bank-statements/{bs_id}")
def get_bank_statement(bs_id: int, db: Session = Depends(get_db)):
    bs = db.query(BankStatements).filter(BankStatements.id == bs_id).first()
    if not bs:
        raise HTTPException(status_code=404, detail="Bank statement not found")
    return _bs_to_dict(bs)

@app.put("/bank-statements/{bs_id}")
def update_bank_statement(bs_id: int, payload: BankStatementInputSchema, db: Session = Depends(get_db)):
    bs = db.query(BankStatements).filter(BankStatements.id == bs_id).first()
    if not bs:
        raise HTTPException(status_code=404, detail="Bank statement not found")
    bs.bank_name = payload.bank_name
    bs.account_number = payload.account_number
    bs.referrence_no = payload.referrence_no
    bs.transaction_date = payload.transaction_date
    bs.description = payload.description
    bs.transaction_type = payload.transaction_type
    bs.amount = payload.amount
    bs.category = payload.category or "Miscellaneous"
    bs.reconciliation_status = payload.reconciliation_status or "pending"
    bs.party_name = payload.party_name or ''
    bs.voucher_ref = payload.voucher_ref or ''
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(bs)
    return _bs_to_dict(bs)

@app.delete("/bank-statements/{bs_id}")
def delete_bank_statement(bs_id: int, db: Session = Depends(get_db)):
    bs = db.query(BankStatements).filter(BankStatements.id == bs_id).first()
    if not bs:
        raise HTTPException(status_code=404, detail="Bank statement not found")
    try:
        db.delete(bs)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    return {"message": "Bank statement deleted successfully"}

def _brs_to_dict(b):
    return {
        "id": b.id,
        "transaction_id": b.transaction_id,
        "voucher_no": b.voucher_no,
        "description": b.description,
        "amount": b.amount,
        "gst_amount": b.gst_amount,
    }

@app.post("/BRS")
def add_BRS(payload: BRSInputSchema, db: Session = Depends(get_db)):
    brs = BRS(
        transaction_id=payload.transaction_id,
        voucher_no=payload.voucher_no,
        description=payload.description,
        amount=payload.amount,
        gst_amount=payload.gst_amount,
    )
    db.add(brs)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(brs)
    return _brs_to_dict(brs)

@app.get("/BRS")
def get_BRS(db: Session = Depends(get_db)):
    rows = db.query(BRS).order_by(BRS.id.desc()).all()
    return [_brs_to_dict(r) for r in rows]

@app.delete("/BRS/{brs_id}")
def delete_BRS(brs_id: int, db: Session = Depends(get_db)):
    brs = db.query(BRS).filter(BRS.id == brs_id).first()
    if not brs:
        raise HTTPException(status_code=404, detail="BRS record not found")
    bs_id     = brs.transaction_id
    voucher_no = brs.voucher_no
    try:
        db.delete(brs)
        # Revert bank statement to pending, clear party and voucher ref
        bs = db.query(BankStatements).filter(BankStatements.id == bs_id).first()
        if bs:
            bs.reconciliation_status = "pending"
            bs.party_name = ''
            bs.voucher_ref = ''
        # Revert voucher status to Pending
        vch = db.query(Vouchers).filter(Vouchers.voucher_no == voucher_no).first()
        if vch:
            vch.status = "Pending"
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    return {"message": "BRS record deleted and related records reverted to pending"}

def _godown_to_dict(g):
    return {
        "id": g.id,
        "godown_name": g.godown_name,
        "location": g.location,
        "items": g.items
    }

@app.post("/godown")
def add_godown(payload: GodownSchema, db: Session = Depends(get_db)):
    new_godown = godown(
        godown_name = payload.godown_name,
        location = payload.location,
        items = payload.items or []
    )
    db.add(new_godown)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(new_godown)
    return new_godown

@app.get("/godown")
def get_godown(db: Session = Depends(get_db)):
    rows = db.query(godown).order_by(godown.id.desc()).all()
    return [_godown_to_dict(r) for r in rows]

def _units_to_dict(u):
    return {
        "id": u.id,
        "symbol": u.symbol,
        "name": u.name,
        "conversion": u.conversion,
        "decimals": u.decimals,
        "type": u.type,
        "used": u.used
    }

@app.put("/godown/{godown_name}")
def update_godown(godown_name:str, payload:GodownSchema, db:Session=Depends(get_db)):
    stock_item: Any
    row = db.query(godown).filter(godown.godown_name == godown_name).first()
    if not row:
        raise HTTPException(status_code=404, detail="Godown not found")
    old_items = copy.deepcopy(row.items) or []
    new_items = payload.items or []

    item_names = list({(item.get('name') or item.get('itemName')) for item in old_items + new_items if (item.get('name') or item.get('itemName'))})

    stock_rows = db.query(stock).filter(stock.item.in_(item_names)).all()
    stock_map = {s.item: s for s in stock_rows}
    
    # Update stock item quantities. If a stock item isn't created yet, we do not throw 400.
    for item in old_items:
        it_name = item.get('name') or item.get('itemName')
        if not it_name:
            continue
        stock_item = stock_map.get(it_name)
        if stock_item:
            stock_item.quantity = max(0, stock_item.quantity - item.get('quantity', 0))
            current_godowns = dict(stock_item.godowns or {})
            current_godowns[godown_name] = max(0, current_godowns.get(godown_name, 0) - item.get('quantity', 0))
            stock_item.godowns = current_godowns

    for item in new_items:
        it_name = item.get('name') or item.get('itemName')
        if not it_name:
            continue
        stock_item = stock_map.get(it_name)
        if stock_item:
            stock_item.quantity += item.get('quantity', 0)
            current_godowns = dict(stock_item.godowns or {})
            current_godowns[godown_name] = current_godowns.get(godown_name, 0) + item.get('quantity', 0)
            stock_item.godowns = current_godowns
    
    row.godown_name = payload.godown_name
    row.location = payload.location
    row.items = payload.items or []

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(row)
    return row

@app.get("/godown/{godown_name}")
def get_single_godown(godown_name: str, db: Session = Depends(get_db)):
    row = db.query(godown).filter(godown.godown_name == godown_name).first()
    if not row:
        raise HTTPException(status_code=404, detail="Godown not found")
    return _godown_to_dict(row)

@app.delete("/godown/{godown_name}")
def delete_godown(godown_name: str, db: Session = Depends(get_db)):
    row = db.query(godown).filter(godown.godown_name == godown_name).first()
    if not row:
        raise HTTPException(status_code=404, detail="Godown not found")

    # For each item stored in this godown, deduct its quantity from stock
    # and remove this godown from the stock's godowns dict
    for item_entry in (row.items or []):
        item_name = item_entry.get("itemName") or item_entry.get("name") or ""
        item_qty = item_entry.get("quantity", 0)
        if not item_name:
            continue
        stock_row = db.query(stock).filter(stock.item == item_name).first()
        if stock_row:
            stock_row.quantity = max(0, (stock_row.quantity or 0) - item_qty)
            current_godowns = dict(stock_row.godowns or {})
            current_godowns.pop(godown_name, None)
            stock_row.godowns = current_godowns

    try:
        db.delete(row)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    return {"message": f"Godown '{godown_name}' deleted successfully"}

@app.put("/stock/{item_name}")
def update_stock(item_name: str, payload: StockSchema, db: Session = Depends(get_db)):
    row = db.query(stock).filter(stock.item == item_name).first()
    if not row:
        raise HTTPException(status_code=404, detail="Stock item not found")

    old_godowns: dict = dict(row.godowns or {})
    new_godowns: dict = dict(payload.godowns or {})

    # Adjust all affected godown records
    all_godown_names = set(old_godowns.keys()) | set(new_godowns.keys())
    for gd_name in all_godown_names:
        gd_row = db.query(godown).filter(godown.godown_name == gd_name).first()
        if not gd_row:
            continue
        items_list: list = list(gd_row.items or [])
        # Remove old entry for this stock item
        items_list = [i for i in items_list if (i.get("itemName") or i.get("name")) != item_name]
        # Add updated entry if quantity > 0 in new payload
        new_qty = new_godowns.get(gd_name, 0)
        if new_qty > 0:
            items_list.append({
                "itemName": payload.item,
                "quantity": new_qty,
                "unit": payload.unit
            })
        gd_row.items = items_list

    row.item = payload.item
    row.quantity = payload.quantity
    row.unit = payload.unit
    row.rate = payload.rate
    row.godowns = payload.godowns
    row.gst_rate = payload.gst_rate
    row.hsn_code = payload.hsn_code

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(row)
    return _items_to_dict(row)

@app.delete("/stock/{item_name}")
def delete_stock(item_name: str, db: Session = Depends(get_db)):
    row = db.query(stock).filter(stock.item == item_name).first()
    if not row:
        raise HTTPException(status_code=404, detail="Stock item not found")

    # Remove this stock item from every godown it belongs to
    for gd_name, gd_qty in (row.godowns or {}).items():
        gd_row = db.query(godown).filter(godown.godown_name == gd_name).first()
        if gd_row:
            items_list = [
                i for i in (gd_row.items or [])
                if (i.get("itemName") or i.get("name")) != item_name
            ]
            gd_row.items = items_list

    try:
        db.delete(row)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    return {"message": f"Stock item '{item_name}' deleted successfully"}

@app.post("/units")
def add_unit(payload: UnitSchema, db: Session = Depends(get_db)):
    new_unit = units(
        symbol = payload.symbol,
        name = payload.name,
        conversion = payload.conversion,
        decimals = payload.decimals,
        type = payload.type,
        used = 0
    )
    db.add(new_unit)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(new_unit)
    return new_unit

@app.get("/units")
def get_units(db: Session = Depends(get_db)):
    rows = db.query(units).order_by(units.id.desc()).all()
    return [_units_to_dict(r) for r in rows]

@app.get("/units/{unit_symbol}")
def get_conversion(unit_symbol: str, db: Session = Depends(get_db)):
    row = db.query(units).filter(units.symbol == unit_symbol).first()
    try:
        if row is not None:
            return row.conversion
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
@app.delete("/units/{unit_symbol}")
def delete_unit(unit_symbol: str, db: Session = Depends(get_db)):
    row = db.query(units).filter(units.symbol == unit_symbol).first()
    try:
        if row is not None:
            if row.type == 'simple' and row.used > 0:
                return {"status": "Unsuccesful operation", "detail": "Simple units can't be deleted unless completely unused. You are using this unit to count some stock items. Kindly check and retry."}
            else:
                db.delete(row)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
def _items_to_dict(i):
    return {
        "id": i.id,
        "item": i.item,
        "quantity": i.quantity,
        "unit": i.unit,
        "rate": i.rate,
        "godowns": i.godowns,
        "gst_rate": i.gst_rate,
        "hsn_code": i.hsn_code
    }

@app.post("/stock")
def add_stock(payload: StockSchema, db: Session = Depends(get_db)):
    new_stock = stock(
        item = payload.item,
        quantity = payload.quantity,
        unit = payload.unit,
        rate = payload.rate,
        godowns = payload.godowns,
        gst_rate = payload.gst_rate,
        hsn_code = payload.hsn_code
    )
    db.add(new_stock)

    # Sync: For every godown specified in the stock item payload, add the item to that godown's items list
    for gd_name, gd_qty in (payload.godowns or {}).items():
        gd_row = db.query(godown).filter(godown.godown_name == gd_name).first()
        if gd_row:
            items_list = list(gd_row.items or [])
            # Filter out existing entries for safety
            items_list = [i for i in items_list if (i.get("itemName") or i.get("name")) != payload.item]
            if gd_qty > 0:
                items_list.append({
                    "itemName": payload.item,
                    "quantity": gd_qty,
                    "unit": payload.unit
                })
            gd_row.items = items_list

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(new_stock)
    return new_stock
    
@app.get("/stock")
def get_stock(db: Session = Depends(get_db)):
    rows = db.query(stock).order_by(stock.id.desc()).all()
    return [_items_to_dict(r) for r in rows]

def _log_to_dict(l):
    return {
        "id": l.id,
        "created_at": l.created_at,
        "detail": l.detail,
    }

@app.post("/notification-log")
def add_log(payload: NotificationLogSchema, db: Session = Depends(get_db)):
    new_log = notificationLogs(
        detail = payload.detail
        
    )
    db.add(new_log)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    db.refresh(new_log)
    return new_log

@app.get("/notification-log")
def get_log(db: Session = Depends(get_db)):
    rows = db.query(notificationLogs).order_by(notificationLogs.id.desc()).all()
    return [_log_to_dict(r) for r in rows]

@app.post("/generate-invoice")
def gen_invoice(payload: InvoiceGenerationSchema):
    from utils.invoice_gen import _DEFAULT_PDF

    voucher_type = payload.voucher_type or "Sales"

    # ── Sales (and legacy) path: build the classic data dict ──────────────────
    if voucher_type == "Sales" and payload.invoice_no and payload.issued_to and payload.items:
        data = {
            "invoice_no":   payload.invoice_no,
            "company_name": payload.company_name,
            "issued_to": {
                "name":    payload.issued_to.name,
                "address": payload.issued_to.address,
                "phone":   payload.issued_to.phone,
                "email":   payload.issued_to.email,
            },
            "items": [
                {"desc": item.desc, "qty": item.qty, "price": item.price}
                for item in payload.items
            ],
            "tax_rate":        payload.tax_rate,
            "payment_details": {
                "bank":         payload.payment_details.bank        if payload.payment_details else "",
                "account_no":   payload.payment_details.account_no  if payload.payment_details else "",
                "account_name": payload.payment_details.account_name if payload.payment_details else "",
            },
        }
        for key in ["issued_date", "due_date"]:
            date_str = getattr(payload, key)
            if not date_str:
                data[key] = date_str
                continue
            parsed = None
            for fmt in ("%Y-%m-%d", "%d %B %Y", "%d-%m-%Y"):
                try:
                    parsed = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            data[key] = parsed or date_str
        generate_invoice(output_path=_DEFAULT_PDF, data=data)

    # ── All other voucher types: pass meta dict to the type-aware dispatcher ──
    else:
        meta = payload.meta or {}
        # Merge top-level convenience fields into meta for the PDF generator
        meta["voucher_type"]  = voucher_type
        meta["company_name"]  = payload.company_name or meta.get("company_name", "")
        meta["invoice_no"]    = payload.invoice_no   or meta.get("voucher_number", "")
        meta["issued_date"]   = payload.issued_date  or meta.get("date", "")
        generate_voucher_pdf(voucher_type=voucher_type, output_path=_DEFAULT_PDF, data=meta)

    if not os.path.exists(_DEFAULT_PDF):
        raise HTTPException(status_code=500, detail="Failed to generate voucher PDF")

    with open(_DEFAULT_PDF, "rb") as f:
        pdf_bytes = f.read()

    clear()
    return Response(content=pdf_bytes, media_type="application/pdf")


@app.post("/sync-invoice-stock")
def sync_invoice_stock(payload: InvoiceSyncSchema, db: Session = Depends(get_db)):
    """
    Deduct (revert=False) or restore (revert=True) stock and godown quantities
    based on the items listed in an invoice.  Each item specifies an exact godown.
    """
    warnings_list = []
    for entry in payload.items:
        item_name   = entry.item_name
        qty         = entry.qty
        godown_name = entry.godown  # may be None for legacy vouchers without a godown field

        stock_row = db.query(stock).filter(stock.item == item_name).first()
        if not stock_row:
            warnings_list.append(f"Stock item '{item_name}' not found – skipped.")
            continue

        # ── Adjust total stock quantity ────────────────────────────────────────
        if payload.revert:
            stock_row.quantity = (stock_row.quantity or 0) + qty
        else:
            stock_row.quantity = max(0, (stock_row.quantity or 0) - qty)

        # ── Adjust the specific godown quantity ────────────────────────────────
        if godown_name:
            current_godowns = dict(stock_row.godowns or {})
            if payload.revert:
                current_godowns[godown_name] = current_godowns.get(godown_name, 0) + qty
            else:
                current_godowns[godown_name] = max(0, current_godowns.get(godown_name, 0) - qty)
            stock_row.godowns = current_godowns

            # Sync the godown table record (items list inside each godown)
            gd_row = db.query(godown).filter(godown.godown_name == godown_name).first()
            if gd_row:
                items_list = list(gd_row.items or [])
                updated    = False
                new_items  = []
                for gd_item in items_list:
                    e_name = gd_item.get('itemName') or gd_item.get('name') or ''
                    if e_name == item_name:
                        new_qty = gd_item.get('quantity', 0)
                        new_qty = (new_qty + qty) if payload.revert else max(0, new_qty - qty)
                        gd_item = dict(gd_item)
                        gd_item['quantity'] = new_qty
                        updated = True
                    new_items.append(gd_item)

                # If reverting and item wasn't found in godown's list, add it back
                if not updated and payload.revert:
                    new_items.append({
                        'itemName': item_name,
                        'quantity': qty,
                        'unit':     stock_row.unit
                    })
                gd_row.items = new_items

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    result: dict = {"message": "Inventory synced successfully"}
    if warnings_list:
        result["warnings"] = warnings_list
    return result

