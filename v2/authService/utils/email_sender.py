import os
from dotenv import load_dotenv
load_dotenv()
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
sender_email = os.getenv("MY_EMAIL")
app_password = os.getenv("APP_PASSWORD")

def email_send(email, otp):
    if not sender_email or not app_password:
        raise ValueError("creds missing in env")
    if not email:
        raise ValueError("email address missing")
    subject="Please verify your email"
    body = f"""
     Hello, thanks for choosing our platform.
     Enter this OTP to login: {otp}
     Happy researching!!
     """
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    server=None
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=10)
        server.starttls()
        server.login(sender_email, app_password)
        server.sendmail(sender_email, email, msg.as_string())
        return True
    except Exception as e:
        raise e
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass



