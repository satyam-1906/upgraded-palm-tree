import os, json, uuid
from dotenv import load_dotenv
load_dotenv()
from kafka import KafkaProducer
producer = KafkaProducer(bootstrap_servers=os.getenv("BOOTSTRAP_SERVER"), value_serializer = lambda x:json.loads(x).encode("utf-8"))
def helper(username, file_name, file_key, source, db, Docs):
    event_id=str(uuid.uuid4())
    db_note = Docs(username=username,event_id=event_id,  file_name=file_name, file_key=file_key, source=source)
    db.add(db_note)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise Exception
    db.refresh(db_note)
    event = {"event_id": event_id, "file_key": file_key, "file_name": file_name, "source": source}
    producer.send("doc-submit", key=event_id.encode(), value=event}
    producer.flush()
