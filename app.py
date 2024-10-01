from flask import Flask, request, abort

from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import *

#======python的函數庫==========
import tempfile, os

import openai
import time
import traceback

import json

import random

import requests

from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from azure.cognitiveservices.vision.computervision.models import OperationStatusCodes
from azure.cognitiveservices.vision.computervision.models import VisualFeatureTypes
from msrest.authentication import CognitiveServicesCredentials

from array import array
from PIL import Image
import sys

from azure.core.credentials import AzureKeyCredential
from azure.ai.language.questionanswering import QuestionAnsweringClient

from datetime import datetime,timezone,timedelta

app = Flask(__name__)

static_tmp_path = os.path.join(os.path.dirname(__file__), 'static', 'tmp')

# Set up LINE_bot
line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))


# OPENAI API Key初始化設定
endpoint = os.getenv('END_POINT')
open_ai_api_key = os.getenv('OpenAI_API_KEY')
open_ai_endpoint = os.getenv('OpenAI_ENDPOINT')
deployment_name = os.getenv('OpenAI_DEPLOY_NAME')
openai.api_base = open_ai_endpoint
headers = {
    "Content-Type": "application/json",
    "api-key": open_ai_api_key,
}


# Set up Language Studio
credential = AzureKeyCredential(os.getenv('AZURE_KEY'))
knowledge_base_project = os.getenv('PROJECT')
deployment = 'production'

# Set up Google Sheets API
import gspread
from oauth2client.service_account import ServiceAccountCredentials as sac
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
SERVICE_ACCOUNT_FILE = 'google_auth.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.getenv('SHEET_ID') # Replace with your Google Sheet ID
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
SHEET_NAME = 'stu'
service = build('sheets', 'v4', credentials=creds)
sh = service.spreadsheets()


# Authenticate with the Azure Computer Vision service
vision_subscription_key = os.getenv('VISION_SUBSCRIPTION_KEY')
vision_endpoint = os.getenv('VISION_ENDPOINT')
computervision_client = ComputerVisionClient(vision_endpoint, CognitiveServicesCredentials(vision_subscription_key))




# 連接Azure Language Studio，查詢知識庫
def QA_response(text):
    client = QuestionAnsweringClient(endpoint, credential)
    with client:
        question=text
        output = client.get_answers(
            question = question,
            project_name=knowledge_base_project,
            deployment_name=deployment
        )
    return output.answers[0].answer


# 連接Azure OpenAI的Chatgpt
def Chatgpt_response(prompt):   

    # Define the payload for the request
    # You can modify the system message and the user prompt as needed
    payload = {
        "model": "gpt-4o-mini",  # You can switch between "gpt-4" or "gpt-3.5-turbo"
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},  # Context setting
            {"role": "user", "content": prompt}  # Replace with your actual prompt
        ],
        "temperature": 0.7,  # Modify this value to adjust the creativity level of the model
        "max_tokens": 1000,  # Control the length of the response
        "top_p": 1.0,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0
    }
    
    # Send the request to OpenAI's API
    response = requests.post(open_ai_endpoint, headers=headers, json=payload)
    
    # Check if the request was successful
    if response.status_code == 200:
        # Parse and print the response from GPT
        result = response.json()
        return result['choices'][0]['message']['content']
    else:
        # Print the error if the request was unsuccessful
        print(f"Error {response.status_code}: {response.text}")
    




# 圖片轉文字
def extract_text_from_image(image_path):
    # Open the image file
    with open(image_path, 'rb') as image_stream:
        # Send the image to Azure Computer Vision for text extraction
        read_operation = computervision_client.read_in_stream(image_stream, raw=True)
    
    # Get the operation location (URL with an ID at the end)
    operation_location = read_operation.headers["Operation-Location"]
    # Extract the ID from the operation location
    operation_id = operation_location.split("/")[-1]
    
    # Wait for the operation to complete
    while True:
        result = computervision_client.get_read_result(operation_id)
        if result.status not in ['notStarted', 'running']:
            break
        time.sleep(1)
    
    # If the result is successful, extract the text
    if result.status == 'succeeded':
        text_results = result.analyze_result.read_results
        extracted_text = ""
        for page in text_results:
            for line in page.lines:
                extracted_text += line.text + "\n"
        return extracted_text
    else:
        return "Text extraction failed."



# 紀錄用戶資料
def write_to_sheet(event):
    _id = event.source.user_id
    profile = line_bot_api.get_profile(_id)
        
    _name = profile.display_name
 
    dt1 = datetime.utcnow().replace(tzinfo=timezone.utc)
    dt2 = dt1.astimezone(timezone(timedelta(hours=8)))
    dt=dt2.strftime("%Y-%m-%d %H:%M:%S")
    values = [[_name,event.message.text,dt ]]
    
    # Prepare the data in the format expected by the API
    body = {
        'values': values
    }

    result = service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=SHEET_NAME,
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',  # This option ensures new rows are inserted
        body=body
    ).execute()  



# 監聽所有來自 /callback 的 Post Request
@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']
    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'



# 處理圖片訊息
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    
    # Get the message ID of the image
    message_id = event.message.id
    
    # Download the image content
    message_content = line_bot_api.get_message_content(message_id)
    directory = "static"
    
    # Check if the directory exists, if not, create it
    if not os.path.exists(directory):
        os.makedirs(directory)
    # Save the image to a file
    image_path = f"static/{message_id}.jpg"
    with open(image_path, 'wb') as f:
        for chunk in message_content.iter_content():
            f.write(chunk)
    
    # Call the Azure Computer Vision API to extract text
    extracted_text = extract_text_from_image(image_path)


    
    try:
        gpt_answer = Chatgpt_response("solve or descript the problem:\n\n"+extracted_text)
        print(gpt_answer)
        _id = event.source.user_id
        profile = line_bot_api.get_profile(_id)
            # 紀錄用戶資料
        _name = profile.display_name
     
        dt1 = datetime.utcnow().replace(tzinfo=timezone.utc)
        dt2 = dt1.astimezone(timezone(timedelta(hours=8)))
        dt=dt2.strftime("%Y-%m-%d %H:%M:%S")
        values = [[_name,gpt_answer,dt ]]
        
        # Prepare the data in the format expected by the API
        body = {
            'values': values
        }
    
        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=SHEET_NAME,
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',  # This option ensures new rows are inserted
            body=body
        ).execute()        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(gpt_answer))
    except:
        print(traceback.format_exc())
        line_bot_api.reply_message(event.reply_token, TextSendMessage('Try later'))        
    
    # Reply to the user with the extracted text
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=extracted_text)
    )


# 處理文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    write_to_sheet(event)
    
    msg = event.message.text
    if msg[0:2]=='習題':
        try:
            QA_answer = QA_response(msg)
            print(QA_answer)
            if QA_answer!='No good match found in KB':
                line_bot_api.reply_message(event.reply_token, TextSendMessage(QA_answer))
        except:
            print(traceback.format_exc())
            line_bot_api.reply_message(event.reply_token, TextSendMessage('QA Error'))
    
    elif msg[0]=='!':
        try:
            gpt_answer = Chatgpt_response(msg)
            print(gpt_answer)
            #position = gpt_answer.find('\n\n')
            # Access the second non-empty line (index 2) and get the characters
            line_bot_api.reply_message(event.reply_token, TextSendMessage(gpt_answer))
        except:
            print(traceback.format_exc())
            line_bot_api.reply_message(event.reply_token, TextSendMessage('Please retry later'))                


@handler.add(PostbackEvent)
def handle_message(event):
    print(event.postback.data)
    write_to_sheet(event)


        
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
