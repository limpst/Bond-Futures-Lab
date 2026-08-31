@echo off
chcp 65001 >nul
REM ============================================================================
REM  G7 국채선물 야간 프로브 — 영국·캐나다를 포함해 전 거래소가 동시에 열리는
REM  유일한 창에서 universe 를 전수 확인한다.
REM
REM  왜 20:30 인가 (거래시간, KST 환산):
REM    CBOT   08:00-06:00    거의 24h
REM    Eurex  08:10-05:00    (01:10-22:00 CET)
REM    ICE    16:00-02:00    (08:00-18:00 London)   <- 8/27 실측 때 닫혀 있었다
REM    MX     15:00-05:00    (02:00-16:00 ET)       <- 8/27 실측 때 닫혀 있었다
REM    OSE    15:30-06:00    야간 세션
REM  -> 20:30 에 시작하면 다섯 곳이 전부 열려 있다.
REM
REM  왜 배치로 도는가:
REM    54종을 한 연결에 걸었을 때 독일이 0 이었는데 3분 뒤 6종으로 걸자 정상
REM    수신됐다(2026-08-27). 초과 구독이 rsp_cd=00000 을 받은 채 조용히
REM    버려지는 것으로 보인다. 그래서 8종씩 끊어 순차로 돈다.
REM
REM  등록 (한 번만):
REM    schtasks /create /tn "BondFuturesLab\G7ProbeNight" ^
REM      /tr "C:\Users\leeli\delta-one-lab\tools\run_g7_probe_night.cmd" ^
REM      /sc weekly /d MON,TUE,WED,THU,FRI /st 20:30 /f
REM
REM  해제:  schtasks /delete /tn "BondFuturesLab\G7ProbeNight" /f
REM  수동:  schtasks /run    /tn "BondFuturesLab\G7ProbeNight"
REM
REM  로그:   tools\logs\g7_probe_YYYYMMDD.log
REM  결과:   reports\g7_bond_feed_YYYYMMDD.json  (날짜별로 남긴다 - 덮지 않는다)
REM  조회 전용 - 주문 TR 은 호출하지 않는다.
REM ============================================================================
setlocal
set "ROOT=%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM 배치당 수신 시간(초)과 배치 크기. 후보 60종 / 배치 8 = 8배치 x 90초 = 12분.
set "SECONDS=90"
set "BATCH=8"

for /f "tokens=2 delims==" %%a in ('wmic os get localdatetime /value ^| find "="') do set "DT=%%a"
set "TODAY=%DT:~0,8%"

set "LOGDIR=%ROOT%\tools\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\g7_probe_%TODAY%.log"
set "OUT=%ROOT%\reports\g7_bond_feed_%TODAY%.json"

echo ============================================ >> "%LOG%"
echo [%date% %time%] G7 야간 프로브 시작 (배치 %BATCH%종 x %SECONDS%초) >> "%LOG%"

pushd "%ROOT%"
"C:\Users\leeli\AppData\Local\Programs\Python\Python312\python.exe" -u tools\probe_g7_bonds_ws.py --seconds %SECONDS% --batch %BATCH% --json "%OUT%" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
popd

echo [%date% %time%] 종료 코드 %RC% >> "%LOG%"

REM 실패는 스케줄러에 그대로 알린다.
endlocal & exit /b %RC%
