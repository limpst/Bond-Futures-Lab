@echo off
REM ============================================================================
REM  LS 아침 토큰 갱신 — 하루 한 번 도는 '서비스 재시작'
REM
REM  LS access token 은 하루 단위로 죽는다. 장 시작 전에 캐시를 버리고 새로
REM  받아 하루를 시작한다. 발급만 하지 않고 실제 TR 을 한 번 때려 '정말 먹히는
REM  토큰'인지까지 확인한다 (발급 성공 != 사용 가능 — 2026-08-24 실측).
REM
REM  다른 수집 작업(08:55)보다 먼저 돌도록 08:30 에 건다.
REM
REM  등록 (한 번만):
REM    schtasks /create /tn "BondFuturesLab\MorningToken" ^
REM      /tr "C:\Users\leeli\bond-futures-lab\tools\run_morning_token.cmd" ^
REM      /sc weekly /d MON,TUE,WED,THU,FRI /st 08:30 /f
REM
REM  해제:  schtasks /delete /tn "BondFuturesLab\MorningToken" /f
REM  확인:  schtasks /query  /tn "BondFuturesLab\MorningToken"
REM  수동:  schtasks /run    /tn "BondFuturesLab\MorningToken"
REM
REM  로그: tools\logs\morning_token.log (gitignore)
REM  조회 전용 — 주문 TR 은 호출하지 않는다.
REM ============================================================================
setlocal
set "ROOT=%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "LOGDIR=%ROOT%\tools\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\morning_token.log"

echo ============================================ >> "%LOG%"
echo [%date% %time%] 아침 토큰 갱신 시작 >> "%LOG%"

pushd "%ROOT%"
"C:\Users\leeli\AppData\Local\Programs\Python\Python312\python.exe" tools\morning_token.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
popd

echo [%date% %time%] 종료 코드 %RC% >> "%LOG%"

REM 실패는 스케줄러에 그대로 알린다 — 토큰이 죽으면 그날 수집이 전부 멈춘다.
endlocal & exit /b %RC%
