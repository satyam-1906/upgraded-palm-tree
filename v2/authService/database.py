from sqlalchemy import Column, create_engine, String, Integer, Boolean, DateTime 
from datetime import datetime 
from sqlalchemy.orm import declarative_base, sessionmaker
import os
from dotenv import load_dotenv
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)
sessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
class Users(Base):
    __tablename__ = "vyomUsers"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String)
    username = Column(String, unique=True)
    password = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    isactive = Column(Boolean, default=False)
    company = Column(String)
    gstin = Column(String)

Base.metadata.create_all(bind=engine)
