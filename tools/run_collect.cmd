@echo off
rem KTB/CME 1분봉 주기 수집 러너 — 작업 스케줄러(BondFuturesLab\CollectMinbars)가
rem 월~금 08:55부터 5분 간격으로 호출한다. 로그는 tools\logs\collect.log (gitignore).
rem --scan-daily: 하루 첫 실행에서 해외 유니버스를 거래량순 스캔(50만 계약+ 전부 편입).
cd /d C:\Users\leeli\bond-futures-lab
echo ---- %date% %time% ---- >> tools\logs\collect.log
"C:\Users\leeli\AppData\Local\Programs\Python\Python312\python.exe" tools\collect_minbars.py --live --scan-daily --count 900 >> tools\logs\collect.log 2>&1
