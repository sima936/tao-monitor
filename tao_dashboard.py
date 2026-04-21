import requests, time, os, sys
from datetime import datetime

API_KEY = "tao-07fa8ae2-9d1d-4d70-8e91-7bb056604211:be6002dd"
HEADERS = {"Authorization": API_KEY}

TARGETS = {"SN0 Root":49.9,"SN64 Chutes":16.5,"SN62 Ridges":10.4,"SN4 Targon":8.7,"SN75 Hippius":6.6,"SN68 Nova":4.9,"SN51 Lium.io":3.0}
STAKES = {0:("SN0 Root",5.99),64:("SN64 Chutes",1.96),62:("SN62 Ridges",1.24),4:("SN4 Targon",1.04),75:("SN75 Hippius",0.79),68:("SN68 Nova",0.58),51:("SN51 Lium.io",0.35)}
HOTKEYS = {"SN0 Root":"5GP7c3fFazW9GXK8Up3qgu2DJBk8inu4aK9TZy3RuoSWVCMi","SN64 Chutes":"5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ","SN62 Ridges":"5Djyacas3eWLPhCKsS3neNSJonzfxJmD3gcrMTFDc4eHsn62","SN4 Targon":"5Hp18g9P8hLGKp9W3ZDr4bvJwba6b6bY3P2u3VdYf8yMR8FM","SN75 Hippius":"5G1Qj93Fy22grpiGKq6BEvqqmS2HVRs3jaEdMhq9absQzs6g","SN68 Nova":"5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg","SN51 Lium.io":"5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u"}
WARN=3.0; ALERT=5.0
R="[0m"; BOLD="[1m"; DIM="[2m"
RED="[91m"; GRN="[92m"; YLW="[93m"; CYN="[96m"; WHT="[97m"
BRED="[41m"; BGRN="[42m"; BYLW="[43m"

def fetch_price():
    try:
        r=requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd",timeout=8)
        return float(r.json()["bittensor"]["usd"])
    except: return None

def fetch_meta():
    try:
        r=requests.get("https://api.taostats.io/api/metagraph/latest/v1",headers=HEADERS,params={"netuid":0,"limit":200},timeout=15)
        vt={}; emi={}
        for n in r.json().get("data",[]):
            hk=n.get("hotkey",{})
            hk=hk.get("ss58",hk) if isinstance(hk,dict) else hk
            vt[str(hk)]=float(n.get("validator_trust",0) or 0)
            emi[str(hk)]=float(n.get("daily_validating_tao",0) or 0) / 1e9
        return vt,emi
    except: return {},{}

def run():
    while True:
        try:
            price=fetch_price()
            vt,emi=fetch_meta()
            os.system("clear")
            total=sum(v for _,v in STAKES.values())
            usd=total*price if price else None
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(BOLD+CYN+"  ┌─────────────────────────────────────────┐")
            print("  │     TAO PORTFOLIO DASHBOARD              │")
            print("  └─────────────────────────────────────────┘"+R)
            usd_str=f"  ~  ${usd:,.2f}" if usd else ""
            px_str=f"  TAO/USD ${price:,.2f}" if price else ""
            print(f"  {BOLD}{WHT}Total: {CYN}{total:.4f} TAO{usd_str}{px_str}{R}   {DIM}{now}{R}")
            print()
            print("  "+DIM+f"{'SUBNET':<16} {'STAKED':>7} {'ACTUAL':>7} {'TARGET':>7} {'DRIFT':>7}  {'VTRUST':>7}  {'STATUS':<12}  {'DAILY TAO':>9}"+R)
            print("  "+DIM+"-"*82+R)
            alerts=[]
            for netuid,(name,staked) in STAKES.items():
                actual=(staked/total)*100
                target=TARGETS[name]
                drift=actual-target
                hk=HOTKEYS.get(name,"")
                v=vt.get(hk,0)
                e=emi.get(hk,0)
                if abs(drift)>=ALERT:
                    ds=RED+BOLD+f"{drift:+.1f}%"+R; st=BRED+BOLD+" REBALANCE"+R; alerts.append(name)
                elif abs(drift)>=WARN:
                    ds=YLW+f"{drift:+.1f}%"+R; st=BYLW+BOLD+"  WATCH   "+R
                else:
                    ds=GRN+f"{drift:+.1f}%"+R; st=BGRN+BOLD+"   OK     "+R
                if v>=0.80: vs=GRN+f"{v:.4f}"+R
                elif v>=0.60: vs=YLW+f"{v:.4f}"+R
                elif v>0: vs=RED+f"{v:.4f}"+R
                else: vs=DIM+"  n/a "+R
                es=CYN+f"{e:>9.4f}"+R if e>0 else DIM+"      n/a"+R
                usd_row=f"${staked*price:>9.2f}" if price else f"{"n/a":>10}"
                us=WHT+usd_row+R
                row=f"  {BOLD}{WHT}{name:<16}{R} {staked:>7.2f} {actual:>6.1f}% {target:>6.1f}%  "+ds+"   "+vs+"  "+st+"  "+es+"  "+us
                print(row)
            print("  "+DIM+"-"*82+R)
            print(f"  {BOLD}{'TOTAL':<16}{R} {total:>7.4f}")
            print()
            if alerts:
                print(BRED+BOLD+"  WARNING: REBALANCE NEEDED -> "+", ".join(alerts)+" "+R)
            else:
                print(BGRN+BOLD+"  ALL POSITIONS WITHIN THRESHOLD "+R)
            print()
            print(DIM+"  Refreshing every 60s  Ctrl+C to exit"+R)
        except KeyboardInterrupt:
            os.system("clear"); print(CYN+"Dashboard closed."+R); sys.exit(0)
        except Exception as ex:
            print(RED+f"Error: {ex}"+R)
        time.sleep(60)

run()
