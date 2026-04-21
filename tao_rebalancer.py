import os,json,time,requests,logging
from datetime import datetime
from pathlib import Path
BOT_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN","")
CHAT_ID=os.environ.get("TELEGRAM_CHAT_ID","")
LOG_DIR=Path(os.path.expanduser("~/tao_logs"))
LOG_FILE=LOG_DIR/"rebalancer.log"
DRIFT_THRESHOLD=0.10
TARGETS={"SN0 Root":49.9,"SN64 Chutes":16.5,"SN62 Ridges":10.4,"SN4 Targon":8.7,"SN75 Hippius":6.6,"SN68 Nova":4.9,"SN51 Lium.io":3.0}
STAKES=[{"subnet":0,"name":"SN0 Root","hotkey":"5GP7c3fFazW9GXK8Up3qgu2DJBk8inu4aK9TZy3RuoSWVCMi","staked":5.99},{"subnet":64,"name":"SN64 Chutes","hotkey":"5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ","staked":1.96},{"subnet":62,"name":"SN62 Ridges","hotkey":"5Djyacas3eWLPhCKsS3neNSJonzfxJmD3gcrMTFDc4eHsn62","staked":1.24},{"subnet":4,"name":"SN4 Targon","hotkey":"5Hp18g9P8hLGKp9W3ZDr4bvJwba6b6bY3P2u3VdYf8yMR8FM","staked":1.04},{"subnet":75,"name":"SN75 Hippius","hotkey":"5G1Qj93Fy22grpiGKq6BEvqqmS2HVRs3jaEdMhq9absQzs6g","staked":0.79},{"subnet":68,"name":"SN68 Nova","hotkey":"5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg","staked":0.58},{"subnet":51,"name":"SN51 Lium.io","hotkey":"5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u","staked":0.35}]
LOG_DIR.mkdir(parents=True,exist_ok=True)
logging.basicConfig(level=logging.INFO,handlers=[logging.FileHandler(LOG_FILE),logging.StreamHandler()])
log=logging.getLogger(__name__)
def send(msg):
 try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":msg},timeout=10)
 except Exception as e: log.error(e)
def fetch_price():
 try:
  r=requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd",timeout=10)
  if r.status_code==200: return float(r.json()["bittensor"]["usd"])
 except: pass
 return None
def run():
 total=sum(s["staked"] for s in STAKES)
 price=fetch_price()
 usd=f" (${total*price:,.2f})" if price else ""
 out=[]
 drifted=[]
 for s in STAKES:
  ap=(s["staked"]/total)*100
  tp=TARGETS[s["name"]]
  drift=ap-tp
  flag=" ** REBALANCE **" if abs(drift)>=DRIFT_THRESHOLD*100 else ""
  out.append(s["name"]+" "+str(round(ap,1))+"% target "+str(tp)+"% drift "+str(round(drift,1))+"%"+flag)
  if flag: drifted.append(s["name"]+" drift="+str(round(drift,2))+"TAO")
 msg="Rebalance Check\nTotal: "+str(round(total,4))+" TAO"+usd+"\n\n"+"\n".join(out)
 if drifted: msg+="\n\nAction needed:\n"+"\n".join(drifted)
 else: msg+="\n\nAll positions within threshold"
 send(msg)
 log.info("Done")
run()

