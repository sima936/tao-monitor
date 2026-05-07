import csv,json,os,requests
from datetime import datetime
COLDKEY="5HR3cMSEnyzQbGCqgeHHQxCosgCBDi6a2tkWiBE3XCwUsmNR"
LOG_FILE="/home/simar/tao_performance.csv"
STATE_FILE="/home/simar/tao_state.json"
BASELINE=11.90
STAKES=[{"subnet":"SN0","hotkey":"5GP7c3fFazW9GXK8Up3qgu2DJBk8inu4aK9TZy3RuoSWVCMi","baseline":5.94},{"subnet":"SN64","hotkey":"5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ","baseline":1.96},{"subnet":"SN62","hotkey":"5Djyacas3eWLPhCKsS3neNSJonzfxJmD3gcrMTFDc4eHsn62","baseline":1.24},{"subnet":"SN4","hotkey":"5Hp18g9P8hLGKp9W3ZDr4bvJwba6b6bY3P2u3VdYf8yMR8FM","baseline":1.04},{"subnet":"SN75","hotkey":"5G1Qj93Fy22grpiGKq6BEvqqmS2HVRs3jaEdMhq9absQzs6g","baseline":0.79},{"subnet":"SN68","hotkey":"5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg","baseline":0.58},{"subnet":"SN51","hotkey":"5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u","baseline":0.35}]
HEADERS=["date","tao_price","total_tao","total_usd","daily_reward_tao","cumulative_tao","SN0","SN64","SN62","SN4","SN75","SN68","SN51"]
def get_price():
 try:
  r=requests.get("https://api.coingecko.com/api/v3/simple/price",params={"ids":"bittensor","vs_currencies":"usd"},timeout=10)
  return r.json()["bittensor"]["usd"]
 except:return None
def load_state():
 try:
  with open(STATE_FILE) as f:return json.load(f)
 except:return {}
def save_state(s):
 with open(STATE_FILE,"w") as f:json.dump(s,f,indent=2)
def take_snapshot():
 if not os.path.exists(LOG_FILE):
  with open(LOG_FILE,"w",newline="") as f:csv.DictWriter(f,fieldnames=HEADERS).writeheader()
 state=load_state();balances={s["subnet"]:s["baseline"] for s in STAKES};total=sum(balances.values());price=get_price();prev=state.get("prev_total_stake",BASELINE)
 row={"date":__import__("datetime").datetime.now().strftime("%Y-%m-%d"),"tao_price":str(round(price,4)) if price else "N/A","total_tao":str(round(total,6)),"total_usd":str(round(total*price,2)) if price else "N/A","daily_reward_tao":"+"+str(round(total-prev,6)) if total>=prev else str(round(total-prev,6)),"cumulative_tao":"+"+str(round(total-BASELINE,6)) if total>=BASELINE else str(round(total-BASELINE,6)),**{s["subnet"]:str(round(balances[s["subnet"]],6)) for s in STAKES}}
 with open(LOG_FILE,"a",newline="") as f:csv.DictWriter(f,fieldnames=HEADERS).writerow(row)
 state["prev_total_stake"]=total;save_state(state)
 print("Snapshot saved - "+str(round(total,4))+" TAO"+(" | $"+str(round(total*price,2)) if price else ""))
def show_summary():
 if not os.path.exists(LOG_FILE):print("No data yet.");return
 with open(LOG_FILE) as f:rows=list(csv.DictReader(f))
 print("Date        Total TAO         USD       Daily       Cumul");print("-"*60)
 for r in rows[-14:]:print(r["date"]+" "+r["total_tao"]+" "+r["total_usd"]+" "+r["daily_reward_tao"]+" "+r["cumulative_tao"])
import sys
cmd=sys.argv[1] if len(sys.argv)>1 else "snapshot"
if cmd=="snapshot":take_snapshot()
elif cmd=="summary":show_summary()
