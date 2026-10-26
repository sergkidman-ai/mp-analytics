# поток: ret
"""Company-scoped WB QR upload and Pult-authenticated pairing pages."""
import base64
import hmac
import html
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response

from returns_bot import wb_qr, wb_qr_followup

load_dotenv(Path(__file__).resolve().parents[1]/'.env')
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
UPLOAD='/api/returns/wb-qr/diskver'
UPLOAD_PATTERN='/api/returns/wb-qr/{company}'
TOKEN_ENV={'wb_acc2':'RETURNS_WB_QR_UPLOAD_TOKEN','wb_acc1':'RETURNS_WB_QR_DIGITAL_UPLOAD_TOKEN'}

def token(account='wb_acc2'):return os.environ.get(TOKEN_ENV[account],'')
def origin_allowed(origin):return bool(re.fullmatch(r'chrome-extension://[a-p]{32}',origin or ''))
def account_for(company):
    account=wb_qr.ACCOUNT_BY_SLUG.get(company)
    if not account:raise HTTPException(404,'unknown_company')
    return account

@app.middleware('http')
async def headers(request,call_next):
    origin=request.headers.get('origin')
    if origin and not origin_allowed(origin):return JSONResponse({'error':'origin'},status_code=403)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    if origin:
        response.headers['Access-Control-Allow-Origin']=origin
        response.headers['Vary']='Origin'
    return response

@app.options(UPLOAD_PATTERN)
def options(company:str):
    account_for(company)
    return Response(status_code=204,headers={'Access-Control-Allow-Methods':'POST, OPTIONS','Access-Control-Allow-Headers':'Authorization, Content-Type'})

@app.post(UPLOAD_PATTERN)
async def upload(company:str,request:Request,background_tasks:BackgroundTasks):
    account=account_for(company)
    expected=token(account)
    if not expected or not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+expected):raise HTTPException(401,'unauthorized')
    raw=await request.body()
    if len(raw)>4_000_000:raise HTTPException(413,'too_large')
    try:
        import json
        data=json.loads(raw)
        png=base64.b64decode(data['png_b64'],validate=True)
        if len(png)>2_500_000:raise ValueError('too_large')
        if not isinstance(data['validity'],str) or len(data['validity'])>250:raise ValueError('invalid_validity')
        meta=wb_qr.store(png,data['day'],data['validity'],account=account)
        background_tasks.add_task(wb_qr_followup.run)
        return {'ok':True,'day':meta['day'],'valid_until':meta['valid_until']}
    except (ValueError,KeyError,TypeError):raise HTTPException(400,'invalid_or_expired_qr') from None

def setup_page(request,account):
    # Nginx injects this header only after the existing Pult BasicAuth check.
    if not request.headers.get('x-pult-user'):raise HTTPException(403,'pult_login_required')
    value=token(account)
    if not value:raise HTTPException(503,'not_configured')
    title=wb_qr.ACCOUNTS[account]['title']
    extension='QR WB — Цифровой' if account=='wb_acc1' else 'QR WB — тест Дисквэр'
    return HTMLResponse('<!doctype html><meta charset="utf-8"><title>Подключение QR WB</title>'
      '<main style="font:18px system-ui;max-width:760px;margin:40px auto"><h1>QR Wildberries - '+html.escape(title)+'</h1>'
      '<p>Скопируйте код подключения в расширение «'+html.escape(extension)+'». Он разрешает загрузку QR только для выбранной компании.</p>'
      '<p>Код не нужно отправлять в Telegram или дропбокс.</p><textarea readonly style="width:100%;height:85px">'+html.escape(value)+'</textarea>'
      '<p>Вернитесь в заказы нужного покупательского аккаунта WB, вставьте код в расширение и нажмите «Включить сбор к отчёту».</p>'
      '<p>QR собирается в 09:00 МСК, понедельник–суббота. Бот присылает его вместе с отчётом после 09:05 МСК. Если компьютер был недоступен, сбор повторяется каждый час до получения QR; поздний QR бот отправит отдельно.</p></main>')

@app.get('/returns/wb-qr/setup',response_class=HTMLResponse)
def setup(request:Request):return setup_page(request,'wb_acc2')

@app.get('/returns/wb-qr/setup/tsifrovoy',response_class=HTMLResponse)
def setup_digital(request:Request):return setup_page(request,'wb_acc1')
