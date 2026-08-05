from fastapi import FastAPI, Depends, Request, HTTPException
import os, json, jwt, hashlib
from argon2 import PasswordHasher
ph = PasswordHasher()
from sqlalchemy.orm import Session
from sqlalchemy.sql import text
from fastapi.responses import Response
from database import sessionLocal, Users
from utils.otp_generator import gen_otp
from utils.email_sender import email_send
from dotenv import load_dotenv
from datetime import datetime, timedelta
load_dotenv()
from argon2.exceptions import VerifyMismatchError
from redis import Redis
from schemas import CreateSchema, VerifySchema, LoginSchema
def get_db():
    db = sessionLocal()
    try:
        yield db
finally:
        db.close()
app = FastAPI()
redis_client = Redis(host=os.getenv("REDIS_HOST"), password = os.getenv("REDIS_PASSWORD"), port = int(os.getenv("REDIS_PORT")), decode_responses = True)

@app.get("/")
def check():
    return {"status": "Running"}

@app.get("/dbcheck")
def dbchek(db:Session=Depends(get_db)):
    db_status = "up"
    try:
        db.execute(text('SELECT 1'))
    except Exception as e:
        db_status = "down"
    return {"db_status": db_status }

@app.get("/redischeck")
def redchek():
    redis_status = "up"
    try:
        redis_client.ping()
    except Exception as e:
        redis_status = "down"
    return {"redis_status": redis_status}

@app.post("/createusers")
def createUs(payload: CreateSchema, db:Session=Depends(get_db)):
    name, email, password, company, username, gstin = payload.name, payload.email, payload.password, payload.company, payload.username, payload.gstin
    db_note = Users(name=name, email=email, password=ph.hash(password), company=company, username=username, gstin=gstin)
    db.add(db_note)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="internal server error")
    db.refresh(db_note)
    verify_otp = gen_otp()
    hashed = hashlib.sha256(otp.encode()).hexdigest()
    redis_client.setex(f"verify_otp:{email}", 600, hashed)
    email_send(email, verify_otp)
    return {"message": "otp sent successfully"}

@app.post("/verify")
def veri(payload:VerifySchema, db:Session=Depends(get_db)):
    email, verify_otp = payload.email, payload.verify_otp
    key = f"verify_otp:{email}"
    hashed=redis_client.get(key)
    if not hashed:
        raise HTTPException(status_code=404, detail="not found")
    input_hash=hashlib.sha256(verify_otp.encode()).hexdigest()
    if input_hash != stored:
        raise HTTPException(status_code=401, detail="otp does not match")
    user = db.query(Users).filter(Users.email==email).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    user.isactive = True
    db.add(user)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"internal server error: {str(e)}")
    db.refresh(user)
    return {"message": "proceed to login"}

@app.post("/login")
def logi(payload:LoginSchema, response: Response, db:Session=Depends(get_db)):
    username, password = payload.username, payload.password
    user = db.query(Users).filter(Users.username==username).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if user.isactive:
        passw = user.password
        try:
            ph.verify(passw, password)
            secret= os.getenv("JWT_SECRET")
            if not secret:
                raise HTTPException(status_code=4-4, detail="secret key not found")
            pay = {"iss": "vyom-auth", "sub": username, "exp": datetime.utcnow()+timedelta(days=7)}
            token = jwt.encode(pay, secret, algorithm="HS256")
            response.set_cookie(key="session_token", value=token, httponly=True, secure=True, samesite="lax", max_age=604800)
            return {"message": "logged in"}
        except VerifyMismatchError:
            raise HTTPException(status_code=401, detail="passwords do not match")
    else:
        raise HTTPException(status_code=403, detail="email not verified")

@app.get("/profile")
def get_profile(request: Request, db:Session=Depends(get_db)):
    session_token = request.cookies.get("session_token")
    if not session_token:
        raise HTTPException(status_code=401, detail="no session token found")
    payl= jwt.decode(session_token, os.getenv("JWT_SECRET"), algorithms=["HS256"])
    username = payl.get("sub")
    user = db.query(Users).filter(Users.username==username).first()
    if not user:
        raise HTTPException(status_code=404, detail="no user found")
    return {"name": user.name, "company": user.company, "username": user.username, "gstin": user.gstin, "created_at": user.created_at}

@app.get("/logout")
def logo(request: Request, response: Response, db:Session=Depends(get_db)):
    session_token= request.cookies.get("session_token")
    if not session_token:
        raise HTTPException(status_code=401, detaail="you are not logged in")
    response.delete_cookie("session_token")
    return {"message": "logged out"}



