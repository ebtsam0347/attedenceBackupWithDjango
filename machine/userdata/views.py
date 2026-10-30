
# table_for_attendance = 'tbl_Attendance_Short'
# table_for_devices = 'PayRoll_payroll_deviceinfo'
from config import table_for_devices


from django.shortcuts import render
from django.http import HttpResponse, StreamingHttpResponse, JsonResponse
from django.db.models import Q
import os
import sys
import zklib
import time
import threading
import queue
import json
import requests as req_lib
from zk import ZK, const
from .models import *
from django.contrib import messages
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import connections, IntegrityError
from django.db import close_old_connections
import pandas as pd
import socket as _socket
from urllib.parse import urlencode

# When output goes to a file Windows uses cp1252, and printing a character it
# cannot encode raises inside the push code. A log line must never stop a push.
# line_buffering: without it a crash loses the last log lines, rejections included.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(errors='backslashreplace', line_buffering=True)

# ── API CONFIG ────────────────────────────────────────────────────────────────
# Set these three environment variables to run against UAT instead of production
PLAYER_API_BASE     = os.environ.get('PLAYER_API_BASE', 'https://www.sialkotfootballacademy.pk')
PLAYER_API_LOGIN    = os.environ.get('PLAYER_API_LOGIN', 'admin@sfa.pk')
PLAYER_API_PASSWORD = os.environ.get('PLAYER_API_PASSWORD', 'F0rward@3456')

# Shared HTTP session — reuses TCP connections (big speed win for repeated calls)
_http_session = req_lib.Session()

# In-memory player info cache — avoids a GET request for every live scan
_player_cache = {}        # {user_id: {'data': {...}|None, 'ts': float}}
_PLAYER_CACHE_TTL = 1800  # 30 minutes
_PLAYER_MISS_TTL  = 60    # failed lookups may just be an API outage — ask again soon

_local_api_token = None

_PULL_LOOKBACK_DAYS = 7  # auto-pull re-reads this many past days so scans missed while the server was off are saved
_DEVICE_FULL_READ_SECONDS = 3600  # a full device log can overwrite old scans without its count changing
_UNSENT_STATUSES = ['pending', '', 'failed']
_player_api_down = False  # set by get_player_info, ends a retry pass early during an outage
# API is unreachable — the rest of the batch would fail the same way
_RETRY_STOP_REASONS = ('Network error', 'Auth token missing', 'Token refresh failed')
# ─────────────────────────────────────────────────────────────────────────────


def _device_reachable(ip, port, timeout=2):
    """Quick TCP-port probe — does NOT do ZK handshake, just checks if port is open."""
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((str(ip), int(port)))
        return True
    except Exception:
        return False
    finally:
        try: s.close()
        except Exception: pass


def _get_local_api_token():
    global _local_api_token
    try:
        r = _http_session.post(
            f'{PLAYER_API_BASE}/api/auth/login',
            json={'login': PLAYER_API_LOGIN, 'password': PLAYER_API_PASSWORD},
            timeout=5
        )
        if r.status_code == 200:
            _local_api_token = r.json().get('accessToken')
            print(f'[AUTH] Token refreshed OK')
        else:
            print(f'[AUTH] Login failed: {r.status_code} {r.text[:100]}')
    except Exception as e:
        print(f'[AUTH] Login exception: {e}')


def push_attendance_to_local_api(user_id, att_datetime, status, device_name='', device_ip=''):
    """Push one attendance record to /api/attendance/players/{user_id}.
    Returns (api_status, api_reason) — 'success'|'failed'|'rejected', reason string.
    'failed' is temporary and gets retried; 'rejected' will never succeed."""
    global _local_api_token

    if not user_id:
        return 'rejected', 'No valid player ID — push skipped'

    if not _local_api_token:
        _get_local_api_token()
    if not _local_api_token:
        return 'failed', 'Auth token missing — login failed'

    # Values read back from the DB are UTC — the attendance date must be local
    push_date   = timezone.localtime(att_datetime).strftime('%Y-%m-%d')
    utc_dt     = att_datetime.astimezone(timezone.utc)
    check_in_at = utc_dt.isoformat().replace('+00:00', 'Z')
    url = f'{PLAYER_API_BASE}/api/attendance/players/{user_id}'
    payload = {
        'date':       push_date,
        'checkInAt':  check_in_at,
        'status':     'PRESENT',
        'source':     'BIOMETRIC',
        'note':       f'{device_name} ({device_ip})'.strip() if device_name or device_ip else '',
    }
    print(f'[PUSH] payload → {json.dumps(payload)}')

    try:
        headers = {'Authorization': f'Bearer {_local_api_token}'}
        r = _http_session.put(url, json=payload, headers=headers, timeout=5)
        print(f'[PUSH] PUT {url} → {r.status_code} | {r.text[:300]}')

        if r.status_code == 401:
            _get_local_api_token()
            if not _local_api_token:
                return 'failed', 'Token refresh failed (401)'
            headers = {'Authorization': f'Bearer {_local_api_token}'}
            r = _http_session.put(url, json=payload, headers=headers, timeout=5)
            print(f'[PUSH] retry PUT {url} → {r.status_code} | {r.text[:200]}')

        if r.status_code in (200, 201, 204):
            return 'success', f'HTTP {r.status_code}: Player #{user_id} | {push_date}'
        try:
            err_detail = r.json().get('message', '') or r.text[:150]
        except Exception:
            err_detail = r.text[:150]
        # Other 4xx mean the API refused this record; retrying cannot help
        reason = f'HTTP {r.status_code}: {err_detail}'
        if 400 <= r.status_code < 500 and r.status_code not in (401, 403, 408, 429):
            print(f'[REJECTED] player={user_id} date={push_date} {reason}')
            return 'rejected', reason
        return 'failed', reason

    except Exception as e:
        print(f'[PUSH] Exception {url}: {e}')
        return 'failed', f'Network error: {str(e)[:150]}'


def get_player_info(user_id):
    """Fetch player info by ZK seq-number. Cached for 30 min per user_id, misses for 60 s."""
    global _player_api_down
    now = time.time()
    cached = _player_cache.get(user_id)
    if cached and (now - cached['ts']) < (_PLAYER_CACHE_TTL if cached['data'] else _PLAYER_MISS_TTL):
        return cached['data']
    try:
        r = _http_session.get(
            f'{PLAYER_API_BASE}/api/public/players/seq/{user_id}', timeout=5
        )
        _player_api_down = False
        if r.status_code == 200:
            data = r.json()
            _player_cache[user_id] = {'data': data, 'ts': now}
            return data
    except Exception:
        _player_api_down = True
    _player_cache[user_id] = {'data': None, 'ts': now}
    return None


_warm_lock = threading.Lock()

def _warm_player_cache(zk_user_ids):
    """Fetch players in the background so a page never waits on the website."""
    if not _warm_lock.acquire(blocking=False):
        return
    try:
        for zk_user_id in zk_user_ids:
            get_player_info(zk_user_id)
            if _player_api_down:
                break
    finally:
        _warm_lock.release()


def _identify_player(zk_user_id):
    """Return the website player for a ZK User ID, or None until the website has
    both a name and a real player ID. The ZK number itself is never a player ID."""
    player = get_player_info(zk_user_id)
    if player and player.get('fullName') and player.get('userId'):
        return player
    return None


def _scan_time(att):
    ts = att.timestamp
    return timezone.make_aware(datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second))


def _day_bounds(first_day, last_day):
    """Local start of first_day and end of last_day. Filtering on these uses the
    att_datetime index; __date wraps the column in SQL and scans the whole table."""
    start = timezone.make_aware(datetime.combine(first_day, datetime.min.time()))
    end   = timezone.make_aware(datetime.combine(last_day + timedelta(days=1), datetime.min.time()))
    return start, end


def _saved_scans(device_ip, first_day):
    """Scans of this device already in the DB, so a pull only touches the new ones."""
    start, _ = _day_bounds(first_day, first_day)
    return set(
        LiveAttendance.objects.filter(
            device_ip=device_ip, zk_user_id__gt='', att_datetime__gte=start,
        ).order_by().values_list('zk_user_id', 'att_datetime')
    )


def save_scan(att, device_ip, device_name):
    """Save one device scan, whoever it belongs to. Returns (record, created).
    Every path that reads a device goes through here, so no scan is ever dropped."""
    zk_user_id = str(att.user_id)
    att_dt = _scan_time(att)
    player = _identify_player(zk_user_id)
    api_user_id = player['userId'] if player else None

    # Rows from before zk_user_id existed can only be matched on the player ID
    same_person = Q(zk_user_id=zk_user_id)
    if api_user_id:
        same_person |= Q(user_id=api_user_id)
    same_device = LiveAttendance.objects.filter(same_person, device_ip=device_ip)

    existing = same_device.filter(att_datetime=att_dt).order_by('-zk_user_id').first()
    if existing:
        if not existing.zk_user_id:
            existing.zk_user_id = zk_user_id
            existing.save(update_fields=['zk_user_id'])
        return existing, False

    defaults = {
        'user_id':     api_user_id,
        'status':      att.status,
        'device_name': device_name or '',
    }
    if player:
        defaults.update(
            full_name = player['fullName'],
            reg_no    = player.get('registrationNo') or '',
            squad     = player.get('squadName') or '',
            slot      = player.get('slot') or player.get('slotName') or '',
        )
    else:
        defaults['api_reason'] = 'Player not registered on website yet'

    # A double tap is kept as a record, but only the first scan is pushed
    if same_device.filter(att_datetime__gte=att_dt - timedelta(seconds=60),
                          att_datetime__lt=att_dt).exists():
        defaults['api_status'] = 'duplicate'
        defaults['api_reason'] = 'Already recorded within 60 seconds'

    return LiveAttendance.objects.get_or_create(
        zk_user_id=zk_user_id, att_datetime=att_dt, device_ip=device_ip,
        defaults=defaults,
    )


def _push_record(rec):
    """Push one saved record and store the outcome. Returns (api_status, api_reason)."""
    api_st, api_rs = push_attendance_to_local_api(
        rec.user_id, rec.att_datetime, rec.status,
        rec.device_name or '', rec.device_ip or ''
    )
    LiveAttendance.objects.filter(id=rec.id).update(api_status=api_st, api_reason=api_rs)
    return api_st, api_rs


def _push_new_scan(rec):
    """Push a just-saved scan when its player is known. The rest wait for the retry loop."""
    if rec.user_id and rec.api_status == 'pending':
        _push_record(rec)


def _push_worker(push_q, sse_q, stop_event):
    """Dedicated push thread per SSE connection — never blocks live capture."""
    while not stop_event.is_set():
        try:
            task = push_q.get(timeout=1)
        except queue.Empty:
            continue
        close_old_connections()
        api_status, api_reason = push_attendance_to_local_api(
            task['user_id'], task['att_datetime'], task['att_status'],
            task['device_name'], task['device_ip']
        )
        try:
            LiveAttendance.objects.filter(id=task['rec_id']).update(
                api_status=api_status, api_reason=api_reason
            )
        except Exception as e:
            print(f'[PUSH WORKER] DB update failed: {e}')
        sse_q.put({
            'type':       'push_update',
            'timestamp':  task['timestamp_str'],
            'api_status': api_status,
            'api_reason': api_reason,
        })


# ─────────────────────────────────────────────────────────────────────────────

device_list_with_ip = []
device_list_with_ip_error = []

def add_device_to_list(device_list_with_ip, DeviceIp):
    if DeviceIp not in device_list_with_ip:
        device_list_with_ip.append(DeviceIp)

def add_error_device_to_list(device_list_with_ip_error, DeviceIp, error_message):
    for i, entry in enumerate(device_list_with_ip_error):
        if entry[0] == DeviceIp:
            device_list_with_ip_error[i] = (DeviceIp, error_message)
            break
    else:
        device_list_with_ip_error.append((DeviceIp, error_message))


# Records page tabs: (key, label, icon, api_status values, message when API gave no reason)
API_TABS = [
    ('all',       'All Scans',   'fa-list-alt',           None,                  ''),
    ('success',   'Sent to API', 'fa-check-circle',       ('success',),          'Accepted by API'),
    ('pending',   'Pending',     'fa-clock-o',            ('pending', ''),       'Waiting to send to API'),
    ('failed',    'Failed',      'fa-exclamation-circle', ('failed', 'rejected'), 'API did not accept this record'),
    ('duplicate', 'Duplicate',   'fa-files-o',            ('duplicate',),        'Same scan already recorded'),
]
API_STATUS_LABELS = {
    'success': 'Sent', 'pending': 'Pending', '': 'Pending',
    'failed': 'Failed', 'rejected': 'Rejected', 'duplicate': 'Duplicate',
}


def attendance_records(request):
    date_str = request.GET.get('date', datetime.now().strftime('%Y-%m-%d'))
    search   = request.GET.get('search', '').strip()
    api_tab  = request.GET.get('api', 'all')
    if api_tab not in [tab[0] for tab in API_TABS]:
        api_tab = 'all'

    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        selected_date = datetime.now().date()
        date_str = selected_date.strftime('%Y-%m-%d')

    records, stats = [], {'total': 0, 'unique': 0, 'sent_percent': 0}
    error_msg = ''
    status_map = {0:'Check-In', 1:'Check-Out', 2:'Break-Out', 3:'Break-In', 4:'OT-In', 5:'OT-Out', 15:'Face-Check-In'}
    tab_counts = {tab[0]: 0 for tab in API_TABS}
    tab_for_status = {status: tab[0] for tab in API_TABS if tab[3] for status in tab[3]}
    tab_reasons = {tab[0]: tab[4] for tab in API_TABS}

    try:
        day_start, day_end = _day_bounds(selected_date, selected_date)
        qs = LiveAttendance.objects.filter(att_datetime__gte=day_start, att_datetime__lt=day_end)

        if search:
            qs = qs.filter(
                Q(full_name__icontains=search)  |
                Q(reg_no__icontains=search)     |
                Q(squad__icontains=search)      |
                Q(user_id__icontains=search)    |
                Q(zk_user_id__icontains=search)
            )

        rows = list(qs)

        # Photo, jersey etc. come from the cache only; anything missing is
        # fetched in the background and shows on the next refresh
        missing = {r.zk_user_id for r in rows
                   if r.zk_user_id and r.user_id and r.zk_user_id not in _player_cache}
        if missing:
            threading.Thread(target=_warm_player_cache, args=(missing,), daemon=True).start()

        for row in rows:
            row_tab = tab_for_status.get(row.api_status, 'pending')
            tab_counts['all'] += 1
            tab_counts[row_tab] += 1
            if api_tab not in ('all', row_tab):
                continue

            player = (_player_cache.get(row.zk_user_id) or {}).get('data') or {}
            local_time = timezone.localtime(row.att_datetime)
            records.append({
                'user_id':     row.user_id or row.zk_user_id,
                'datetime':    local_time.strftime('%Y-%m-%d %H:%M:%S'),
                'time_only':   local_time.strftime('%I:%M:%S %p'),
                'api_tab':     row_tab,
                'api_label':   API_STATUS_LABELS.get(row.api_status, row.api_status),
                'api_reason':  row.api_reason or tab_reasons[row_tab],
                'status':      row.status,
                'status_label': status_map.get(row.status, str(row.status)),
                'device_name': row.device_name,
                'device_ip':   row.device_ip,
                'full_name':   row.full_name or player.get('fullName', ''),
                'squad':       row.squad     or player.get('squadName', ''),
                'reg_no':      row.reg_no    or player.get('registrationNo', ''),
                'photo_url':   (PLAYER_API_BASE.rstrip('/') + '/' + player['photoUrl'].lstrip('/')) if player.get('photoUrl') else '',
                'jersey_no':   player.get('jerseyNo') or '',
                'position':    player.get('position') or '',
                'blood_group': player.get('bloodGroup') or '',
                'phone':       player.get('phone') or '',
            })

        stats['total']  = len(rows)
        stats['unique'] = len({r.user_id or r.zk_user_id for r in rows})
        # Duplicates are never pushed, so they do not count against the sent share
        sendable = tab_counts['all'] - tab_counts['duplicate']
        stats['sent_percent'] = round(tab_counts['success'] * 100 / sendable) if sendable else 0

    except Exception as e:
        error_msg = str(e)

    api_tabs = []
    for key, label, icon, _, _ in API_TABS:
        params = {'date': date_str, 'api': key}
        if search:
            params['search'] = search
        api_tabs.append({
            'key': key, 'label': label, 'icon': icon, 'count': tab_counts[key],
            'url': '?' + urlencode(params), 'active': key == api_tab,
        })

    return render(request, 'userdata/attendance_records.html', {
        'records':       records,
        'stats':         stats,
        'api_tabs':      api_tabs,
        'api_tab':       api_tab,
        'send_status':   send_status(),
        'selected_date': date_str,
        'search':        search,
        'error_msg':     error_msg,
    })


def live_attendance_page(request):
    return render(request, 'userdata/live_attendance.html')


def live_attendance_stream(request):
    def event_stream():
        q        = queue.Queue()
        push_q   = queue.Queue()
        stop_event = threading.Event()

        # Dedicated push worker — API calls happen here, not in capture thread
        pw = threading.Thread(target=_push_worker, args=(push_q, q, stop_event), daemon=True)
        pw.start()

        def capture_from_device(device_ip, port=4370):
            retry_delay = 15  # seconds; doubles on each failure, capped at 120s
            while not stop_event.is_set():
                conn = None
                close_old_connections()
                try:
                    zk = ZK(device_ip, port=port, timeout=5)
                    conn = zk.connect()
                    device_name = conn.get_device_name()
                    retry_delay = 15  # reset backoff on successful connect
                    q.put({'type': 'connected', 'device_ip': device_ip, 'device_name': device_name})
                    none_count = 0  # consecutive None yields from live_capture
                    for attendance in conn.live_capture(new_timeout=10):
                        if stop_event.is_set():
                            break
                        if attendance is None:
                            # live_capture yields None on socket.timeout (10 s of silence).
                            # After 3 consecutive Nones (30 s) we probe the TCP port.
                            # If the port is closed the device is offline → break → retry loop.
                            none_count += 1
                            if none_count >= 3:
                                if not _device_reachable(device_ip, port):
                                    raise Exception('Device port unreachable — offline or network issue')
                                none_count = 0  # port open → device alive but idle
                            continue
                        none_count = 0  # real scan received → device definitely alive
                        if attendance:
                            # Cache hit = instant; cache miss = 1 HTTP GET (then cached)
                            player = _identify_player(attendance.user_id) or {}
                            timestamp_str = attendance.timestamp.strftime('%Y-%m-%d %H:%M:%S')

                            api_status, api_reason = 'pending', 'Sending to API...'
                            try:
                                rec, created = save_scan(attendance, device_ip or '', device_name)
                                if not created:
                                    api_status = 'duplicate'
                                    api_reason = 'Exact record already exists'
                                elif rec.user_id and rec.api_status == 'pending':
                                    push_q.put({
                                        'rec_id':        rec.id,
                                        'user_id':       rec.user_id,
                                        'att_datetime':  rec.att_datetime,
                                        'att_status':    attendance.status,
                                        'device_name':   device_name or '',
                                        'device_ip':     device_ip or '',
                                        'timestamp_str': timestamp_str,
                                    })
                                else:
                                    api_status, api_reason = rec.api_status, rec.api_reason
                            except Exception as db_err:
                                api_status = 'failed'
                                api_reason = f'DB error: {str(db_err)[:120]}'
                                print(f'[DB] {device_ip}: {db_err}')

                            q.put({
                                'type':        'attendance',
                                'user_id':     attendance.user_id,
                                'timestamp':   timestamp_str,
                                'status':      attendance.status,
                                'device_ip':   device_ip,
                                'device_name': device_name,
                                'full_name':   player.get('fullName', ''),
                                'squad':       player.get('squadName', ''),
                                'reg_no':      player.get('registrationNo', ''),
                                'photo_url':   (PLAYER_API_BASE.rstrip('/') + '/' + player['photoUrl'].lstrip('/')) if player.get('photoUrl') else '',
                                'blood_group': player.get('bloodGroup') or '',
                                'phone':       player.get('phone') or '',
                                'dob':         player.get('dob') or '',
                                'gender':      player.get('gender') or '',
                                'jersey_no':   str(player.get('jerseyNo')) if player.get('jerseyNo') else '',
                                'position':    player.get('position') or '',
                                'school':      player.get('schoolName') or '',
                                'city':        player.get('city') or '',
                                'fee_paid':    player.get('feePaidThrough') or '',
                                'nationality': player.get('nationality') or '',
                                'api_status':  api_status,
                                'api_reason':  api_reason,
                            })
                except Exception as e:
                    if stop_event.is_set():
                        break
                    print(f'[DEVICE {device_ip}] Offline: {e} — retry in {retry_delay}s')
                    q.put({'type': 'reconnecting', 'device_ip': device_ip,
                           'delay': retry_delay, 'message': str(e)})
                    waited = 0
                    while waited < retry_delay and not stop_event.is_set():
                        time.sleep(1)
                        waited += 1
                    retry_delay = min(retry_delay * 2, 120)
                finally:
                    try:
                        if conn:
                            conn.disconnect()
                    except Exception:
                        pass

        try:
            devices = PayRoll_payroll_deviceinfo.objects.filter(IsActive=True)
            device_list = [(d.DeviceIp, d.Port) for d in devices if d.DeviceIp]
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'device_ip': '', 'message': str(e)})}\n\n"
            return

        if not device_list:
            yield f"data: {json.dumps({'type': 'error', 'device_ip': '', 'message': 'No active devices found in database'})}\n\n"
            return

        for device_ip, port in device_list:
            t = threading.Thread(target=capture_from_device, args=(device_ip, port), daemon=True)
            t.start()

        try:
            while True:
                try:
                    data = q.get(timeout=20)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        except GeneratorExit:
            stop_event.set()

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


def auto_attendance_page(request):
    return render(request, 'userdata/auto_attendance.html')


_pull_lock = threading.Lock()
_last_pull = {'results': [], 'total_saved': 0, 'timestamp': None}
_device_reads = {}  # {device_ip: {'records': int, 'ts': float}} — last complete read


def do_pull_from_devices():
    """Save new device scans. Only one pull runs at a time; a second caller gets the last result."""
    global _last_pull
    if not _pull_lock.acquire(blocking=False):
        return _last_pull
    try:
        _last_pull = _pull_from_devices()
        return _last_pull
    finally:
        _pull_lock.release()


def _pull_from_devices():
    today = datetime.now().date()
    earliest = today - timedelta(days=_PULL_LOOKBACK_DAYS)
    results = []
    total_saved = 0

    try:
        close_old_connections()
        devices = PayRoll_payroll_deviceinfo.objects.filter(IsActive=True)
        device_list = list(devices)
    except Exception as e:
        return {'error': str(e), 'results': [], 'total_saved': 0,
                'timestamp': datetime.now().strftime('%H:%M:%S')}

    for d in device_list:
        conn = None
        try:
            zk = ZK(d.DeviceIp, port=d.Port or 4370, timeout=8)
            conn = zk.connect()
            dev_name = conn.get_device_name() or d.DeviceName

            # Downloading the whole log is the slow part, so skip it when nothing changed
            conn.read_sizes()
            last_read = _device_reads.get(d.DeviceIp)
            if (last_read and last_read['records'] == conn.records
                    and time.time() - last_read['ts'] < _DEVICE_FULL_READ_SECONDS):
                results.append({'ip': d.DeviceIp, 'name': dev_name, 'status': 'ok', 'saved': 0})
                continue

            attendances = conn.get_attendance()
            known = _saved_scans(d.DeviceIp, earliest)
            saved = 0
            all_saved = True
            for att in attendances:
                if (att.timestamp.date() >= earliest
                        and (str(att.user_id), _scan_time(att)) not in known):
                    try:
                        rec, created = save_scan(att, d.DeviceIp, dev_name)
                        if created:
                            saved += 1
                            _push_new_scan(rec)
                    except Exception as e:
                        all_saved = False
                        print(f'[PULL] {d.DeviceIp} user={att.user_id} {att.timestamp} not saved: {e}')
            if all_saved:
                _device_reads[d.DeviceIp] = {'records': conn.records, 'ts': time.time()}
            total_saved += saved
            results.append({'ip': d.DeviceIp, 'name': dev_name, 'status': 'ok', 'saved': saved})
        except Exception as e:
            results.append({'ip': d.DeviceIp, 'name': d.DeviceName, 'status': 'error', 'error': str(e)})
        finally:
            try:
                if conn:
                    conn.disconnect()
            except Exception:
                pass

    return {
        'results':     results,
        'total_saved': total_saved,
        'timestamp':   datetime.now().strftime('%H:%M:%S'),
        'date':        today.strftime('%Y-%m-%d'),
    }


# Background auto-pull every 5 minutes (runs even when browser is closed)
def _background_pull_loop():
    time.sleep(30)  # let the server finish startup, then catch up on anything missed
    while True:
        try:
            do_pull_from_devices()
            print(f'[AUTO-PULL] {datetime.now().strftime("%H:%M:%S")} — done')
        except Exception as e:
            print(f'[AUTO-PULL] Error: {e}')
        time.sleep(5 * 60)

_bg_thread = threading.Thread(target=_background_pull_loop, daemon=True)
_bg_thread.start()


def _attach_player(rec, player):
    """Fill in the real player on a waiting scan, which makes it ready to push."""
    api_user_id = player['userId']
    already_saved = LiveAttendance.objects.filter(
        user_id=api_user_id,
        att_datetime=rec.att_datetime,
        device_ip=rec.device_ip,
    ).exclude(id=rec.id).exists()
    if already_saved:
        rec.api_status = 'duplicate'
        rec.api_reason = 'Same scan already saved with the real player ID'
        rec.save(update_fields=['api_status', 'api_reason'])
        return

    rec.user_id   = api_user_id
    rec.full_name = player.get('fullName') or ''
    rec.reg_no    = player.get('registrationNo') or ''
    rec.squad     = player.get('squadName') or ''
    rec.slot      = player.get('slot') or player.get('slotName') or ''
    rec.save(update_fields=['user_id', 'full_name', 'reg_no', 'squad', 'slot'])


_retry_lock = threading.Lock()  # the 2-minute loop and the Send Now button never overlap
_last_retry = {'finished_at': '', 'sent': 0, 'failed': 0, 'rejected': 0, 'waiting': 0, 'problem': ''}


def _identify_waiting_scans():
    """One lookup per ZK User ID, however many scans are waiting on it.
    Returns False when the player API could not be reached."""
    waiting = LiveAttendance.objects.filter(
        api_status__in=_UNSENT_STATUSES, user_id__isnull=True, zk_user_id__gt='',
    )
    for zk_user_id in list(waiting.order_by().values_list('zk_user_id', flat=True).distinct()):
        player = _identify_player(zk_user_id)
        if _player_api_down:
            print('[RETRY] Player API unreachable — lookups stop until next pass')
            return False
        if player:
            for rec in list(waiting.filter(zk_user_id=zk_user_id)):
                try:
                    _attach_player(rec, player)
                except Exception as e:
                    print(f'[RETRY] record={rec.id} error: {e}')
    return True


def _retry_pass():
    """One pass over every record the API has not accepted yet, oldest first."""
    close_old_connections()
    summary = {'sent': 0, 'failed': 0, 'rejected': 0, 'waiting': 0, 'problem': ''}
    if not _identify_waiting_scans():
        summary['problem'] = 'Website could not be reached'
    ready = list(
        LiveAttendance.objects.filter(
            api_status__in=_UNSENT_STATUSES, user_id__isnull=False,
        ).order_by('att_datetime')
    )
    if ready:
        print(f'[RETRY] {len(ready)} unsent record(s) — retrying push...')
    for rec in ready:
        try:
            api_st, api_rs = _push_record(rec)
            print(f'[RETRY] user={rec.user_id} → {api_st}')
            summary['sent' if api_st == 'success' else api_st] += 1
            if api_st == 'failed' and api_rs.startswith(_RETRY_STOP_REASONS):
                print('[RETRY] API unreachable — stopping until next pass')
                summary['problem'] = api_rs[:150]
                break
        except Exception as e:
            print(f'[RETRY] record={rec.id} error: {e}')
            summary['failed'] += 1
            summary['problem'] = str(e)[:150]

    summary['waiting'] = LiveAttendance.objects.filter(
        api_status__in=_UNSENT_STATUSES, user_id__isnull=True,
    ).count()
    return summary


def _run_retry_pass():
    """Caller must hold _retry_lock. Never raises, so a bad pass cannot end the loop."""
    global _last_retry
    try:
        summary = _retry_pass()
    except Exception as e:
        print(f'[RETRY LOOP] Error: {e}')
        summary = {**_last_retry, 'problem': str(e)[:150]}
    finally:
        _retry_lock.release()
    summary['finished_at'] = datetime.now().strftime('%I:%M:%S %p')
    _last_retry = summary


def retry_unsent_pushes():
    """Run one pass now, unless a pass is already running."""
    if _retry_lock.acquire(blocking=False):
        _run_retry_pass()


def _retry_in_background():
    _run_retry_pass()
    connections.close_all()


def send_status():
    """What the Send Now bar shows."""
    try:
        unsent = LiveAttendance.objects.filter(api_status__in=_UNSENT_STATUSES).count()
    except Exception:
        unsent = 0
    return {**_last_retry, 'running': _retry_lock.locked(), 'unsent': unsent}


def api_send_pending(request):
    """GET: progress of the send. POST: start sending now, without waiting for the 2-minute loop."""
    if request.method == 'POST' and _retry_lock.acquire(blocking=False):
        threading.Thread(target=_retry_in_background, daemon=True).start()
    return JsonResponse(send_status())


def _retry_pending_loop():
    """Retry unsent attendance records every 2 minutes."""
    time.sleep(90)  # initial delay so server finishes startup
    while True:
        retry_unsent_pushes()
        time.sleep(2 * 60)  # check every 2 minutes

_retry_thread = threading.Thread(target=_retry_pending_loop, daemon=True)
_retry_thread.start()


def api_auto_pull(request):
    """Latest pull result. Only the background thread reads devices, so this page never waits on them."""
    return JsonResponse(_last_pull)


def do_pull_back_data(from_date, to_date):
    """Pull attendance from all ZK devices for a given date range and save to LiveAttendance.
    Saved scans are pushed by the retry loop, so this page does not wait on the website."""
    results = []
    total_saved = 0

    try:
        close_old_connections()
        devices = PayRoll_payroll_deviceinfo.objects.filter(IsActive=True)
        device_list = list(devices)
    except Exception as e:
        return {'error': str(e), 'results': [], 'total_saved': 0}

    for d in device_list:
        conn = None
        try:
            zk = ZK(d.DeviceIp, port=d.Port or 4370, timeout=10)
            conn = zk.connect()
            dev_name = conn.get_device_name() or d.DeviceName
            attendances = conn.get_attendance()
            known = _saved_scans(d.DeviceIp, from_date)
            saved = 0
            skipped = 0
            for att in attendances:
                att_date = att.timestamp.date()
                if not (from_date <= att_date <= to_date):
                    continue
                if (str(att.user_id), _scan_time(att)) in known:
                    skipped += 1
                    continue
                try:
                    _, created = save_scan(att, d.DeviceIp, dev_name)
                    if created:
                        saved += 1
                    else:
                        skipped += 1
                except Exception as e:
                    print(f'[BACK-DATA] {d.DeviceIp} user={att.user_id} {att.timestamp} not saved: {e}')
            total_saved += saved
            results.append({
                'ip': d.DeviceIp, 'name': dev_name, 'status': 'ok',
                'saved': saved, 'skipped': skipped,
                'total_in_range': saved + skipped,
            })
        except Exception as e:
            results.append({'ip': d.DeviceIp, 'name': d.DeviceName, 'status': 'error', 'error': str(e)})
        finally:
            try:
                if conn:
                    conn.disconnect()
            except Exception:
                pass

    return {
        'results':     results,
        'total_saved': total_saved,
        'from_date':   from_date.strftime('%Y-%m-%d'),
        'to_date':     to_date.strftime('%Y-%m-%d'),
        'timestamp':   datetime.now().strftime('%H:%M:%S'),
    }


def back_data_page(request):
    today_str = datetime.now().strftime('%Y-%m-%d')
    from_str  = request.GET.get('from_date', today_str)
    to_str    = request.GET.get('to_date',   today_str)
    search    = request.GET.get('search', '').strip()
    pull_result = None
    pull_error  = ''

    try:
        from_date = datetime.strptime(from_str, '%Y-%m-%d').date()
        to_date   = datetime.strptime(to_str,   '%Y-%m-%d').date()
    except ValueError:
        from_date = to_date = datetime.now().date()
        from_str  = to_str  = today_str

    if request.method == 'POST':
        fd = request.POST.get('from_date', today_str)
        td = request.POST.get('to_date',   today_str)
        try:
            from_date = datetime.strptime(fd, '%Y-%m-%d').date()
            to_date   = datetime.strptime(td, '%Y-%m-%d').date()
            from_str, to_str = fd, td
            if from_date > to_date:
                pull_error = 'From date cannot be after To date.'
            else:
                pull_result = do_pull_back_data(from_date, to_date)
        except ValueError:
            pull_error = 'Invalid date format.'

    status_map = {0:'Check-In',1:'Check-Out',2:'Break-Out',3:'Break-In',4:'OT-In',5:'OT-Out',15:'Face-Check-In'}

    range_start, range_end = _day_bounds(from_date, to_date)
    qs = LiveAttendance.objects.filter(att_datetime__gte=range_start, att_datetime__lt=range_end)
    if search:
        qs = qs.filter(
            Q(full_name__icontains=search)  |
            Q(reg_no__icontains=search)     |
            Q(squad__icontains=search)      |
            Q(user_id__icontains=search)    |
            Q(zk_user_id__icontains=search)
        )

    records = []
    for row in qs:
        records.append({
            'user_id':      row.user_id or row.zk_user_id,
            'datetime':     row.att_datetime.strftime('%Y-%m-%d %H:%M:%S'),
            'date_only':    row.att_datetime.strftime('%Y-%m-%d'),
            'time_only':    row.att_datetime.strftime('%I:%M:%S %p'),
            'status':       row.status,
            'status_label': status_map.get(row.status, str(row.status)),
            'device_name':  row.device_name,
            'device_ip':    row.device_ip,
            'full_name':    row.full_name,
            'squad':        row.squad,
            'reg_no':       row.reg_no,
            'api_status':   row.api_status,
            'api_reason':   row.api_reason,
        })

    stats = {
        'total':    len(records),
        'check_in': sum(1 for r in records if r['status'] == 0),
        'check_out':sum(1 for r in records if r['status'] == 1),
        'unique':   len({r['user_id'] for r in records}),
    }

    return render(request, 'userdata/back_data.html', {
        'records':     records,
        'stats':       stats,
        'from_date':   from_str,
        'to_date':     to_str,
        'search':      search,
        'pull_result': pull_result,
        'pull_error':  pull_error,
        'send_status': send_status(),
    })


def api_today_records(request):
    """Return today's LiveAttendance records as JSON for the auto-attendance table."""
    today = datetime.now().date()
    status_map = {0: 'Check-In', 1: 'Check-Out', 2: 'Break-Out', 3: 'Break-In', 4: 'OT-In', 5: 'OT-Out'}

    day_start, day_end = _day_bounds(today, today)
    qs = LiveAttendance.objects.filter(
        att_datetime__gte=day_start, att_datetime__lt=day_end,
    ).order_by('-att_datetime')[:500]
    records = []
    for r in qs:
        records.append({
            'user_id':     r.user_id or r.zk_user_id,
            'full_name':   r.full_name or f'ID:{r.user_id or r.zk_user_id}',
            'reg_no':      r.reg_no,
            'squad':       r.squad,
            'time':        r.att_datetime.strftime('%I:%M %p'),
            'status':      status_map.get(r.status, str(r.status)),
            'device_name': r.device_name,
        })

    return JsonResponse({'records': records, 'count': len(records)})


def api_test_push(request):
    """Diagnose API connectivity: auth, player info, and attendance push."""
    results = {'tests': {}, 'player_cache_size': len(_player_cache)}

    # 1. Server reachability
    try:
        r = _http_session.get(PLAYER_API_BASE, timeout=5)
        results['tests']['server_reachable'] = {'ok': True, 'http_status': r.status_code}
    except Exception as e:
        results['tests']['server_reachable'] = {'ok': False, 'error': str(e)}

    # 2. Auth login
    token = None
    try:
        r = _http_session.post(
            f'{PLAYER_API_BASE}/api/auth/login',
            json={'login': PLAYER_API_LOGIN, 'password': PLAYER_API_PASSWORD},
            timeout=5
        )
        if r.status_code == 200:
            token = r.json().get('accessToken', '')
            results['tests']['auth_login'] = {'ok': True, 'token_received': bool(token)}
        else:
            results['tests']['auth_login'] = {
                'ok': False, 'http_status': r.status_code, 'response': r.text[:200]
            }
    except Exception as e:
        results['tests']['auth_login'] = {'ok': False, 'error': str(e)}

    # 3. Player info API
    uid = request.GET.get('uid', 1)
    player_api_id = None
    try:
        r = _http_session.get(f'{PLAYER_API_BASE}/api/public/players/seq/{uid}', timeout=5)
        results['tests']['player_info'] = {
            'ok': r.status_code == 200, 'http_status': r.status_code, 'seq_id': uid,
        }
        if r.status_code == 200:
            p = r.json()
            player_api_id = p.get('userId')
            results['tests']['player_info']['name'] = p.get('fullName', '')
            results['tests']['player_info']['player_api_id'] = player_api_id
    except Exception as e:
        results['tests']['player_info'] = {'ok': False, 'error': str(e)}

    # 4. Attendance push
    if token and player_api_id:
        try:
            headers = {'Authorization': f'Bearer {token}'}
            payload = {
                'date':   datetime.now().strftime('%Y-%m-%d'),
                'status': 'PRESENT',
                'note':   'Connectivity test from attendance machine',
            }
            r = _http_session.put(
                f'{PLAYER_API_BASE}/api/attendance/players/{player_api_id}',
                json=payload, headers=headers, timeout=5
            )
            results['tests']['attendance_push'] = {
                'ok':          r.status_code in (200, 201, 204),
                'http_status': r.status_code,
                'url':         f'/api/attendance/players/{player_api_id}',
                'response':    r.text[:300],
            }
        except Exception as e:
            results['tests']['attendance_push'] = {'ok': False, 'error': str(e)}
    else:
        results['tests']['attendance_push'] = {
            'ok': False,
            'skipped': 'Need valid auth token + player API id to test push'
        }

    # 5. Recent DB records with push status
    try:
        recent = LiveAttendance.objects.order_by('-created_at')[:10]
        results['recent_db_records'] = [
            {
                'user_id':    r.user_id or r.zk_user_id,
                'full_name':  r.full_name,
                'time':       r.att_datetime.strftime('%Y-%m-%d %H:%M'),
                'api_status': r.api_status,
                'api_reason': r.api_reason,
            }
            for r in recent
        ]
    except Exception as e:
        results['recent_db_records'] = {'error': str(e)}

    return JsonResponse(results, json_dumps_params={'indent': 2})


def devices_page(request):
    msg = ''
    msg_type = ''

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            name = request.POST.get('DeviceName', '').strip()
            ip   = request.POST.get('DeviceIp',   '').strip()
            port = request.POST.get('Port', '4370').strip() or '4370'
            loc  = request.POST.get('Location',   '').strip()
            if not name or not ip:
                msg, msg_type = 'Device name aur IP required hain.', 'error'
            else:
                try:
                    PayRoll_payroll_deviceinfo.objects.create(
                        DeviceName=name, DeviceIp=ip,
                        Port=int(port), Location=loc, IsActive=True
                    )
                    msg, msg_type = f'Device "{name}" add ho gaya.', 'success'
                except Exception as e:
                    msg, msg_type = f'Error: {e}', 'error'

        elif action == 'delete':
            did = request.POST.get('device_id')
            try:
                dev = PayRoll_payroll_deviceinfo.objects.get(pk=did)
                dev.delete()
                msg, msg_type = 'Device delete ho gaya.', 'success'
            except Exception as e:
                msg, msg_type = f'Error: {e}', 'error'

        elif action == 'toggle':
            did = request.POST.get('device_id')
            try:
                dev = PayRoll_payroll_deviceinfo.objects.get(pk=did)
                dev.IsActive = not dev.IsActive
                dev.save(update_fields=['IsActive'])
                state = 'Active' if dev.IsActive else 'Inactive'
                msg, msg_type = f'Device "{dev.DeviceName}" ab {state} hai.', 'success'
            except Exception as e:
                msg, msg_type = f'Error: {e}', 'error'

        elif action == 'edit':
            did  = request.POST.get('device_id')
            name = request.POST.get('DeviceName', '').strip()
            ip   = request.POST.get('DeviceIp',   '').strip()
            port = request.POST.get('Port', '4370').strip() or '4370'
            loc  = request.POST.get('Location',   '').strip()
            if not name or not ip:
                msg, msg_type = 'Device name aur IP required hain.', 'error'
            else:
                try:
                    dev = PayRoll_payroll_deviceinfo.objects.get(pk=did)
                    dev.DeviceName = name
                    dev.DeviceIp   = ip
                    dev.Port       = int(port)
                    dev.Location   = loc
                    dev.save(update_fields=['DeviceName', 'DeviceIp', 'Port', 'Location'])
                    msg, msg_type = f'Device "{name}" update ho gaya.', 'success'
                except Exception as e:
                    msg, msg_type = f'Error: {e}', 'error'

    devices = PayRoll_payroll_deviceinfo.objects.all().order_by('DeviceName')
    return render(request, 'userdata/devices.html', {
        'devices':  devices,
        'msg':      msg,
        'msg_type': msg_type,
    })


def userdata(request):
    if request.method == 'POST':
        date = request.POST.get('date')
        if date:
            try:
                start_date = datetime.strptime(date, '%Y-%m-%d')

                with connections['default'].cursor() as cursor:
                    cursor.execute(f"SELECT DeviceIp FROM {table_for_devices}")
                    rows = cursor.fetchall()
                device_list = [row[0] for row in rows if row[0] is not None]

                for DeviceIp in device_list:
                    try:
                        zk = ZK(DeviceIp, port=4370, timeout=5)
                        conn = zk.connect()
                        if conn:
                            DeviceName = conn.get_device_name()
                            attendances = conn.get_attendance()
                            known = _saved_scans(DeviceIp or '', start_date.date())

                            for attendance in attendances:
                                if (start_date <= attendance.timestamp
                                        and (str(attendance.user_id), _scan_time(attendance)) not in known):
                                    try:
                                        _, created = save_scan(attendance, DeviceIp or '', DeviceName)
                                        if created:
                                            add_device_to_list(device_list_with_ip, DeviceIp)
                                    except Exception as e:
                                        print(f'Error for {DeviceIp}: {e}')
                                        add_error_device_to_list(device_list_with_ip_error, DeviceIp, str(e))
                            conn.disconnect()
                    except Exception as e:
                        add_error_device_to_list(device_list_with_ip_error, DeviceIp, str(e))
                        continue

            except ValueError:
                context = {}
                messages.warning(request, "Invalid date.")
                return render(request, 'userdata/index.html', context)
        else:
            context = {}
            messages.warning(request, "Invalid date.")
            return render(request, 'userdata/index.html', context)

    context = {
        'device_list_with_ip':       device_list_with_ip,
        'device_list_with_ip_error': device_list_with_ip_error,
    }
    return render(request, 'userdata/index.html', context)
