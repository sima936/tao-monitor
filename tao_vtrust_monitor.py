import os,json,time,requests,logging
from datetime import datetime
from pathlib import Path
BOT_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN","")
CHAT_ID=os.environ.get("TELEGRAM_CHAT_ID","")
LOG_DIR=Path(os.path.expanduser("~/tao_logs"))
STATE_FILE=LOG_DIR/"vtrust_state.json"
LOG_FILE=LOG_DIR/"vtrust_monitor.log"
VTRUST_THRESHOLD=0.80
ALERT_COOLDOWN=86400
STAKES=[{"subnet":0,"name":"SN0 Root","hotkey":"5GP7c3fFazW9GXK8Up3qgu2DJBk8inu4aK9TZy3RuoSWVCMi","staked":5.94},{"subnet":64,"name":"SN64 Chutes","hotkey":"5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ","staked":1.96},{"subnet":62,"name":"SN62 Ridges","hotkey":"5Djyacas3eWLPhCKsS3neNSJonzfxJmD3gcrMTFDc4eHsn62","staked":1.24},{"subnet":4,"name":"SN4 Targon","hotkey":"5Hp18g9P8hLGKp9W3ZDr4bvJwba6b6bY3P2u3VdYf8yMR8FM","staked":1.04},{"subnet":75,"name":"SN75 Hippius","hotkey":"5G1Qj93Fy22grpiGKq6BEvqqmS2HVRs3jaEdMhq9absQzs6g","staked":0.79},{"subnet":68,"name":"SN68 Nova","hotkey":"5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg","staked":0.58},{"subnet":51,"name":"SN51 Lium.io","hotkey":"5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u","staked":0.35}]
LOG_DIR.mkdir(parents=True,exist_ok=True)
logging.basicConfig(level=logging.INFO,handlers=[logging.FileHandler(LOG_FILE),logging.StreamHandler()])
log=logging.getLogger(__name__)
def send(msg):
 try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":msg},timeout=10)
 except Exception as e: log.error(e)
def get_vt(subnet,hotkey):
 try:
  r=requests.get(f"https://api.taostats.io/api/metagraph/latest/v1?netuid={subnet}&hotkey={hotkey}",headers={"Authorization":os.environ.get("TAOSTATS_API_KEY","")},timeout=15)
  if r.status_code==200:
   d=r.json().get("data",[])
   if d and d[0].get("validator_trust") is not None: return float(d[0]["validator_trust"])
 except Exception as e: log.warning(e)
 return None
def load_state():
 try: return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_alerted":{}}
 except: return {"last_alerted":{}}
def run():
 state,now,ok=load_state(),time.time(),[]
 for s in STAKES:
  vt=get_vt(s["subnet"],s["hotkey"])
  if vt is None: ok.append("? "+s["name"]+" unavailable"); continue
  if vt<VTRUST_THRESHOLD:
   if now-state["last_alerted"].get(s["hotkey"],0)<ALERT_COOLDOWN: continue
   send("VTrust ALERT: "+s["name"]+" score="+str(round(vt,4))+" below "+str(VTRUST_THRESHOLD))
   state["last_alerted"][s["hotkey"]]=now
  else: ok.append(s["name"]+" ok "+str(round(vt,4)))
 STATE_FILE.write_text(json.dumps(state,indent=2))
 send("VTrust OK: "+"  ".join(ok))
 log.info("Done")
run()
