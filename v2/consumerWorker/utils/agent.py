from langgraph.graph import StateGraph, END
from pydantic import BaseModel, SecretStr, Field
from typing import Optional
from langchain_groq import ChatGroq
from dotenv import load_dotenv
import os
import json
import uuid
import pandas as pd
from io import StringIO
import boto3
load_dotenv()
llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0,
    max_tokens=None,
    reasoning_format="hidden",
    timeout=None,
    max_retries=2,
    api_key=SecretStr(os.getenv("GROQ_API_KEY", "none"))
)
s3 = boto3.client(
    "s3",
    region_name=os.getenv("S3_REGION"),
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
)
bucket = os.getenv("S3_BUCKET_NAME")
class State(BaseModel):
    content: str
    normal: Optional[dict] = None
    fin: Optional[str] = ""
    key: Optional[str] = ""
SCHEMA = """
{
   "invoice_number": "",
   "invoice_date": "",
   "invoice_type": "",
   "supplier_gstin": "",
   "supplier_name": "",
   "place_of_supply": "",
   "hsn_sac_code" : "",
   "item_description": "",
   "quantity": "",
   "unit_price": "",
   "taxable_value": "",
   "gst_rate" : "",
   "cgst_amount": "",
   "sgst_amount": "",
   "igst_amount": "",
   "cess_amount": "",
   "total_invoice_value": "",
   "reverse_charge": ""
}
"""

class LLMSchema(BaseModel):
    invoice_number: str = Field(default="NA")
    invoice_date: str = Field(default="NA")
    invoice_type: str = Field(default="NA")
    supplier_gstin: str = Field(default="NA")
    supplier_name: str = Field(default="NA")
    place_of_supply: str = Field(default="NA")
    hsn_sac_code: str = Field(default="NA")
    item_description: str = Field(default="NA")
    quantity: str = Field(default="NA")
    unit_price: str = Field(default="NA")
    taxable_value: str = Field(default="NA")
    gst_rate: str = Field(default="NA")
    cgst_amount: str = Field(default="NA")
    sgst_amount: str = Field(default="NA")
    igst_amount: str = Field(default="NA")
    cess_amount: str = Field(default="NA")
    total_invoice_value: str = Field(default="NA")
    reverse_charge: str = Field(default="NA")

def clean_json_response(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[len("```json"):]
    if text.startswith("```"):
        text = text[len("```"):]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return text

def normal_node(state: State):
    prompt = f"""You are an invoice extraction system. 
Extract invoice information according to the requested format.
Invoice Text:
{state.content}"""

    # Bind the structured output schema directly to your LLM execution
    structured_llm = llm.with_structured_output(LLMSchema)
    
    try:
        # This will return a parsed Pydantic object automatically!
        structured_resp = structured_llm.invoke(prompt)
        # Convert to standard Python dict for your State
        normal_data = structured_resp.model_dump()
    except Exception as e:
        print("Structured extraction failed:", e)
        raise ValueError(f"LLM failed to provide valid data: {str(e)}")

    return {"normal": normal_data}

def final_node(state: State):
    data = state.normal

    if data is None:
        raise ValueError("No extracted invoice data found")

    invoice_row = {
        "invoice_number": data.get("invoice_number"),
        "invoice_date": data.get("invoice_date"),
        "supplier_name": data.get("supplier", {}).get("name"),
        "buyer_name": data.get("buyer", {}).get("name"),
        "grand_total": data.get("totals", {}).get("grand_total")
    }

    buffer = StringIO()

    pd.DataFrame([invoice_row]).to_csv(
        buffer,
        index=False
    )

    filename = f"{uuid.uuid4()}.csv"
    s3_key = f"processed-csv/{filename}"

    s3.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=buffer.getvalue(),
        ContentType="text/csv"
    )

    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": s3_key
        },
        ExpiresIn=3600
    )

    return {
        "fin": presigned_url,
        "key": s3_key
    }


graph = StateGraph(State)

graph.add_node("normal", normal_node)
graph.add_node("final", final_node)

graph.set_entry_point("normal")

graph.add_edge("normal", "final")
graph.add_edge("final", END)

lang_app = graph.compile()

