#!/usr/bin/env bash
# 운영 서버에서 실행: bash deploy_redesign.sh <패키지.tar.gz>
# 순서: 서비스 중지 → DB 백업(검증) → 코드 백업 → 새 코드 반영 → pip → migrate.py → 서비스 시작
# 중간에 실패하면 서비스를 다시 켜고 멈춘다. 롤백 명령은 마지막에 출력된다.
set -euo pipefail

PKG="${1:?사용법: bash deploy_redesign.sh <패키지.tar.gz>}"
APP=/opt/eml_analyzer
SERVICE=eml-analyzer
UNIT=/etc/systemd/system/$SERVICE.service
APP_USER=uxis218
TS=$(date +%F_%H%M)
BK="${BACKUP_DIR:-$HOME/eml_backup}"
mkdir -p "$BK"

# root로 실행하면 sudo 없이, 앱 파일·pip·마이그레이션은 서비스 계정(uxis218)으로 실행
if [ "$(id -u)" = 0 ]; then SUDO=""; AS_APP="runuser -u $APP_USER --"; else SUDO="sudo"; AS_APP=""; fi
sudo() { if [ -z "$SUDO" ]; then "$@"; else command sudo "$@"; fi; }

# .env 값 읽기 (윈도우 줄바꿈 CR, 앞뒤 따옴표 제거)
envval() {
    local v
    v=$(grep -E "^$1=" "$APP/.env" | tail -1 | cut -d= -f2- | tr -d '\015')
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "$v"
}

trap 'echo; echo "!! 배포 중 오류 — 서비스를 다시 시작합니다"; sudo systemctl start $SERVICE || true' ERR

[ -f "$PKG" ] || { echo "패키지 없음: $PKG"; exit 1; }

echo "[1/7] 서비스 중지"
sudo systemctl stop $SERVICE

echo "[2/7] DB 백업 → $BK/db_$TS.sql.gz"
DB_NAME=$(envval DB_NAME); DB_USER=$(envval DB_USER)
DB_HOST=$(envval DB_HOST); DB_PORT=$(envval DB_PORT)
MYSQL_PWD="$(envval DB_PASSWORD)" mysqldump --single-transaction --default-character-set=utf8mb4 \
    -h "${DB_HOST:-localhost}" -P "${DB_PORT:-3306}" -u "$DB_USER" "$DB_NAME" | gzip > "$BK/db_$TS.sql.gz"
zcat "$BK/db_$TS.sql.gz" | tail -1 | grep -q "Dump completed" || { echo "DB 백업 검증 실패"; false; }
SIZE=$(du -h "$BK/db_$TS.sql.gz" | cut -f1)
echo "      DB 백업 확인 ($SIZE)"

echo "[3/7] 현재 코드 백업 → $BK/app_$TS.tgz"
tar czf "$BK/app_$TS.tgz" --exclude=venv --exclude=attachment_store -C /opt eml_analyzer

echo "[4/7] 새 코드 반영"
tar xzf "$PKG" -C "$APP" --exclude=.env --no-same-owner
if [ -z "$SUDO" ]; then chown -R "$APP_USER:$APP_USER" "$APP" --from=root:root; fi

echo "[5/7] 패키지 설치"
$AS_APP "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "[6/7] DB 마이그레이션"
(cd "$APP" && $AS_APP env EML_DISABLE_BACKGROUND=1 "$APP/venv/bin/python" migrate.py)

echo "[7/7] 서비스 설정 확인 후 시작"
if ! grep -q -- '--timeout' "$UNIT"; then
    sudo sed -i 's/--workers 1/--workers 1 --timeout 120/' "$UNIT"
    sudo systemctl daemon-reload
    echo "      gunicorn --timeout 120 추가"
fi
sudo systemctl start $SERVICE
trap - ERR
sleep 6
systemctl --no-pager --lines=0 status $SERVICE | head -4
echo
echo "최근 로그:"
sudo journalctl -u $SERVICE --no-pager -n 15
echo
echo "완료. 롤백이 필요하면:"
echo "  systemctl stop $SERVICE && tar xzf $BK/app_$TS.tgz -C /opt && systemctl start $SERVICE"
