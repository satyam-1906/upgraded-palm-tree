from sqlalchemy import Column, create_engine, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
import os
from dotenv import load_dotenv
from datetime import datetime
load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))
sessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
class Docs(Base):
    __tablename__ = "docs"
    id = Column(Integer, index=True, autoincrement=True, primary_key=True)
    username = Column(String)
    event_id = Column(String, unique=True)
    file_name=Column(String)
    file_key=Column(String)
    source = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)
