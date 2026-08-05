import os,json, boto3
from datetime import datetime, timedelta
from kafka import KafkaConsumer, KafkaProducer
from utils.extractor import extract, extract_ocr
from utils.agent import lang_app
from botocore.config import Config
from dotenv import load_dotenv
s3 = boto3.client('s3', region_name=os.getenv("S3_REGION"), aws_access_key=os.getenv("AWS_ACCESS_KEY_ID"), aws_secret_access_key_id=os.getenv("AWS_ACCESS_SECRET_KEY_ID"), config=Config(signature_version="s3v4", s3={'adressing_style': 'virtual'}))
load_dotenv()
producer = KafkaProducer(bootstrap_servers=os.getenv("BOOTSTRAP_SERVER"), value_serializer = lambda x: json.dumps(x).encode())
consumer = KafkaConsumer("doc-submit", bootstrap_servers=os.getenv("BOOTSTRAP_SERVER"), value_deserializer= lambda x: json.loads(x.decode()), group_id="doc-workers")
for msg in consumer:
    data = msg.value
    file_key, source = data["file_key"], data["source"]
    response = s3.get_object(Bucket=os.getenv("S3_BUCKET"), Key=file_key)
    file_bytes = response["Body"].read()
    text = extract(file_bytes)
    if len(text.strip())<100:
        text = extract_ocr(file_bytes)
    result = lang_app.invoke(State(content=text))
    text_hash = hash_text(text)
    event = {"event_id" : data["event_id"], "normal" : result["normal"], "csv_file" : result["fin"], "source" : source}
    if source == "whatsapp":
        producer.send("extract-whatsapp", key=data["event_id"].encode(), value=event}
        producer.flush()
    if source == "web_frontend":
        producer.send("extract-web", key=data["event_id"].encode(), value=event}
        producer.flush()
