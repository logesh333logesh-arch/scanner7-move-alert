# Scanner 7 - Move Start Alert

## Setup (Termux)
```
mkdir -p ~/scanner7/.github/workflows && cd ~/scanner7
# move_alert.py, requirements.txt, README.md -> ~/scanner7/
# move_alert.yml -> ~/scanner7/.github/workflows/
git init && git add . && git commit -m "scanner7" && git branch -M main
gh repo create scanner7-move-alert --public --source=. --push
gh secret set TELEGRAM_BOT_TOKEN --repo <user>/scanner7-move-alert
gh secret set TELEGRAM_CHAT_ID  --repo <user>/scanner7-move-alert
gh workflow run move_alert.yml -f test=true
```
No Upstox token needed (uses Yahoo data via yfinance).

## Notes
- Public repo = unlimited free Actions minutes. Private repo: 10-min cron may exceed the 2000 free min/month.
- Crude/NatGas/Gold/Silver use international futures (CL, NG, GC, SI) as the direction proxy for MCX.
- Index volume is not available on Yahoo, so indices use squeeze + EMA50 confirmation; futures/crypto also use volume.
- Data can lag 1-2 min and GitHub cron can run a few minutes late; alerts re-check the last 3 candles so none are missed.
- Tune thresholds at the top of move_alert.py.
