from fastapi import FastAPI, HTTPException, Form, Request, Depends
from sqlalchemy.orm import Session
from database import sessionLocal, Docs
from sqlalchemy.sql import text
from fastapi.responses import Response
import time, requests, jwt, os, uuid, boto3
from dotenv import load_dotenv
load_dotenv()
from utils.kafka_db_helper import helper
app = FastAPI()
s3= boto3.client('s3', region_name=os.getenv("S3_REGION"), aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"), aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"), config=Config(signature_version="s3v4", s3={'addressing_style': 'virtual'}))
bucket = os.getenv("S3_BUCKET")
def get_db():
    db=sessionLocal()
    try:
        yield db
    finally:
        db.close()

EXCLUDED_PATHS = ["/", "/docs", "/metrics", "/dbcheck", "/openapi.json", "/twilio/webhook"]
@app.middleware("http")
async def middle(request: Request, call_next):
    if request.url.path in EXCLUDED_PATHS:
        await return call_next(request)
    auth = request.cookies.get("session_token")
    if not auth:
        raise HTTPException(status_code=401, detail="no auth token found")
    try:
        payl = jwt.decode(auth, os.getenv("JWT_SECRET"), algorithms=["HS256"])
        request.state.username = payl.get("sub")
    except Exception as e:
        raise HTTPException(status_code=401, detail="error decoding auth")
    start = time.time()
    resp = await call_next(request)
    process_time = round(time.time()-start)
    return resp

@app.get("/")
def chek():
    return {"status": "Running"}

@app.get("/dbcheck")
def db_check(db:Session=Depends(get_db)):
    db_status = "up"
    try:
        db.execute(text('SELECT 1'))
    except Exception as e:
        db_status = "down"
    return {"db_status" : db_status}

@app.post("/upload")
def file_up(payload: UploadSchema, db:Session=Depends(get_db)):
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="not logged in")
    file_id=str(uuid.uuid4())
    file_key = f"docs/{username}-{file_id}-{payload.file_name}"
    pres = s3.generate_presigned_url(ClientMethod="put_object", Params={'Bucket':bucket, 'key': file_key, 'ContentType': payload.content_type}, ExpiresIn=600)
    helper(username=username, file_name=payload.file_name, file_key=file_key, source="web_frontend", db=db, Docs=Docs)
    return {"message" : "queued", "presigned_url": pres}

@app.post("/twilio/webhook")
async def twilio_wwb(request: Request, From: str= Form(...), NumMedia:int=Form(0), MediaUrl0:str=Form(None), MediaContentType0:str=Form(None), db:Session=Depends(get_db)):
    if NumMedia==0 or not MediaUrl0:
        return Response(content="<Response></Response>", media_type="application/xml")
    sender_phone = From.replace("whatsapp:", "").strip()
    username = f"{sender_phone}"
    try:
        auth = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")) if os.getenv("TWILIO_ACCOUNT_SID") else None
        file_res = requests.get(MediaUrl0, auth=auth, stream=True)
        if file_res.status_code != 200:
            return Response(content="<Response></Response>", media_type="application/xml")
        file_id = str(uuid.uuid4())
        ext = "pdf" if "pdf" in (MediaContentType0 or "") else "bin"
        file_name = f"whatsapp_invoice_{file_id[:8]}.{ext}"
        key = f"docs/{file_id}-{file_name}-{sender_phone}"
        s3.upload_fileobj(Fileobj=file_res.raw, Bucket=bucket, Key=key,ExtraArgs={"ContentType": MediaContentType0 or "application/pdf"})
        helper(username=username, file_name=file_name, file_key=key, source="whatsapp", db=db,Docs=Docs)
    except Exception as e:
        return Response(content="<Response></Response>", media_type="application/xml")
    return Response(content="<Response></Response>", media_type="application/xml")
