from pydantic import BaseModel
class CreateSchema(BaseModel):
    usename:str
    password:str
    email:str
    name:str
    company:str
    gstin:str

class VerifySchema(BaseModel):
    email:str
    verify_otp:str

class LoginSchema(BaseModel):
    username:str
    password:str
