
# table_for_attendance = 'tbl_Attendance_Short'
# table_for_devices = 'PayRoll_payroll_deviceinfo'
from config import table_for_devices


from django.shortcuts import render
from django.http import HttpResponse, StreamingHttpResponse, JsonResponse
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

# ── API CONFIG ────────────────────────────────────────────────────────────────
PLAYER_API_BASE = 'https://www.sialkotfootballacademy.pk'

# Shared HTTP session — reuses TCP connections (big speed win for repeated calls)
_http_session = req_lib.Session()

# In-memory player info cache — avoids a GET request for every live scan
_player_cache = {}        # {user_id: {'data': {...}|None, 'ts': float}}
_PLAYER_CACHE_TTL = 1800  # 30 minutes

_local_api_token = None
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
            json={'login': 'admin@sfa.pk', 'password': 'F0rward@3456'},
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
    Returns (api_status, api_reason) — 'sent'|'failed', reason string."""
    global _local_api_token

    if not user_id:
        return 'failed', 'No valid player ID — push skipped'

    if not _local_api_token:
        _get_local_api_token()
    if not _local_api_token:
        return 'failed', 'Auth token missing — login failed'

    push_date   = att_datetime.strftime('%Y-%m-%d')
    utc_dt      = att_datetime.astimezone(timezone.utc)
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
            return 'sent', f'Player #{user_id} | {push_date}'
        try:
            err_detail = r.json().get('message', '') or r.text[:150]
        except Exception:
            err_detail = r.text[:150]
        return 'failed', f'HTTP {r.status_code}: {err_detail}'

    except Exception as e:
        print(f'[PUSH] Exception {url}: {e}')
        return 'failed', f'Network error: {str(e)[:150]}'


def get_player_info(user_id):
    """Fetch player info by ZK seq-number. Cached for 30 min per user_id."""
    now = time.time()
    cached = _player_cache.get(user_id)
    if cached and (now - cached['ts']) < _PLAYER_CACHE_TTL:
        return cached['data']
    try:
        r = _http_session.get(
            f'{PLAYER_API_BASE}/api/public/players/seq/{user_id}', timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            _player_cache[user_id] = {'data': data, 'ts': now}
            return data
    except Exception:
        pass
    _player_cache[user_id] = {'data': None, 'ts': now}
    return None


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


def attendance_records(request):
    date_str = request.GET.get('date', datetime.now().strftime('%Y-%m-%d'))
    search   = request.GET.get('search', '').strip()

    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        selected_date = datetime.now().date()
        date_str = selected_date.strftime('%Y-%m-%d')

    records, stats = [], {'total': 0, 'check_in': 0, 'check_out': 0, 'unique': 0}
    error_msg = ''
    status_map = {0:'Check-In', 1:'Check-Out', 2:'Break-Out', 3:'Break-In', 4:'OT-In', 5:'OT-Out', 15:'Face-Check-In'}

    try:
        qs = LiveAttendance.objects.filter(att_datetime__date=selected_date)

        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(full_name__icontains=search) |
                Q(reg_no__icontains=search)    |
                Q(squad__icontains=search)     |
                Q(user_id__icontains=search)
            )

        unique_ids = list(qs.values_list('user_id', flat=True).distinct())
        player_cache_local = {}
        for uid in unique_ids:
            p = get_player_info(uid)
            if p:
                player_cache_local[str(uid)] = p

        for row in qs:
            player = player_cache_local.get(str(row.user_id), {})
            records.append({
                'user_id':     row.user_id,
                'datetime':    row.att_datetime.strftime('%Y-%m-%d %H:%M:%S'),
                'time_only':   row.att_datetime.strftime('%I:%M:%S %p'),
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

        stats['total']    = len(records)
        stats['check_in'] = sum(1 for r in records if r['status'] == 0)
        stats['check_out']= sum(1 for r in records if r['status'] == 1)
        stats['unique']   = len({r['user_id'] for r in records})

    except Exception as e:
        error_msg = str(e)

    return render(request, 'userdata/attendance_records.html', {
        'records':       records,
        'stats':         stats,
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
                            player = get_player_info(attendance.user_id)
                            if not player or not player.get('fullName'):
                                print(f'[SKIP] Unknown user_id {attendance.user_id}')
                                continue
                            ts = attendance.timestamp
                            att_datetime = timezone.make_aware(
                                datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second)
                            )
                            api_user_id = player.get('userId') or attendance.user_id
                            timestamp_str = att_datetime.strftime('%Y-%m-%d %H:%M:%S')

                            api_status, api_reason = 'pending', 'Sending to API...'
                            try:
                                window_start = att_datetime - timedelta(seconds=60)
                                already_exists = LiveAttendance.objects.filter(
                                    user_id=api_user_id,
                                    device_ip=device_ip or '',
                                    att_datetime__gte=window_start,
                                    att_datetime__lte=att_datetime,
                                ).exists()
                                if not already_exists:
                                    rec, created = LiveAttendance.objects.get_or_create(
                                        user_id     = api_user_id,
                                        att_datetime= att_datetime,
                                        device_ip   = device_ip or '',
                                        defaults={
                                            'status':      attendance.status,
                                            'device_name': device_name or '',
                                            'full_name':   player.get('fullName') or '',
                                            'reg_no':      player.get('registrationNo') or '',
                                            'squad':       player.get('squadName') or '',
                                            'slot':        player.get('slot') or player.get('slotName') or '',
                                        }
                                    )
                                    if created:
                                        push_q.put({
                                            'rec_id':        rec.id,
                                            'user_id':       api_user_id,
                                            'att_datetime':  att_datetime,
                                            'att_status':    attendance.status,
                                            'device_name':   device_name or '',
                                            'device_ip':     device_ip or '',
                                            'timestamp_str': timestamp_str,
                                        })
                                    else:
                                        api_status = 'duplicate'
                                        api_reason = 'Exact record already exists'
                                else:
                                    api_status = 'duplicate'
                                    api_reason = 'Already recorded within 60 seconds'
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


def do_pull_from_devices():
    """Core pull logic — runs from background thread OR view."""
    today = datetime.now().date()
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
            attendances = conn.get_attendance()
            saved = 0
            for att in attendances:
                if att.timestamp.date() >= today:
                    try:
                        ts = att.timestamp
                        att_dt = timezone.make_aware(
                            datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second)
                        )
                        player = get_player_info(att.user_id) or {}
                        api_user_id = player.get('userId') or att.user_id
                        window_start = att_dt - timedelta(seconds=60)
                        already_exists = LiveAttendance.objects.filter(
                            user_id=api_user_id,
                            device_ip=d.DeviceIp,
                            att_datetime__gte=window_start,
                            att_datetime__lte=att_dt,
                        ).exists()
                        if not already_exists:
                            rec, created = LiveAttendance.objects.get_or_create(
                                user_id     =api_user_id,
                                att_datetime=att_dt,
                                device_ip   =d.DeviceIp,
                                defaults={
                                    'status':      att.status,
                                    'device_name': dev_name or '',
                                    'full_name':   player.get('fullName') or '',
                                    'reg_no':      player.get('registrationNo') or '',
                                    'squad':       player.get('squadName') or '',
                                    'slot':        player.get('slot') or player.get('slotName') or '',
                                }
                            )
                            if created:
                                saved += 1
                                api_st, api_rs = push_attendance_to_local_api(
                                    api_user_id, att_dt, att.status,
                                    dev_name or '', d.DeviceIp
                                )
                                rec.api_status = api_st
                                rec.api_reason = api_rs
                                rec.save(update_fields=['api_status', 'api_reason'])
                    except Exception:
                        pass
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
    while True:
        time.sleep(5 * 60)
        try:
            do_pull_from_devices()
            print(f'[AUTO-PULL] {datetime.now().strftime("%H:%M:%S")} — done')
        except Exception as e:
            print(f'[AUTO-PULL] Error: {e}')

_bg_thread = threading.Thread(target=_background_pull_loop, daemon=True)
_bg_thread.start()


def _retry_pending_loop():
    """Retry any attendance records stuck in 'pending' status every 2 minutes."""
    time.sleep(90)  # initial delay so server finishes startup
    while True:
        try:
            close_old_connections()
            from datetime import date as _date
            today = _date.today()
            pending = list(
                LiveAttendance.objects.filter(
                    api_status__in=['pending', ''],
                    att_datetime__date=today,
                ).values('id', 'user_id', 'att_datetime', 'status', 'device_name', 'device_ip')
            )
            if pending:
                print(f'[RETRY] {len(pending)} pending record(s) — retrying push...')
            for rec in pending:
                try:
                    api_st, api_rs = push_attendance_to_local_api(
                        rec['user_id'], rec['att_datetime'], rec['status'],
                        rec['device_name'] or '', rec['device_ip'] or ''
                    )
                    LiveAttendance.objects.filter(id=rec['id']).update(
                        api_status=api_st, api_reason=api_rs
                    )
                    print(f'[RETRY] user={rec["user_id"]} → {api_st}')
                except Exception as e:
                    print(f'[RETRY] user={rec["user_id"]} error: {e}')
        except Exception as e:
            print(f'[RETRY LOOP] Error: {e}')
        time.sleep(2 * 60)  # check every 2 minutes

_retry_thread = threading.Thread(target=_retry_pending_loop, daemon=True)
_retry_thread.start()


def api_auto_pull(request):
    """Pull today's attendance from all ZK devices. Also runs automatically in background."""
    data = do_pull_from_devices()
    if 'error' in data and not data.get('results'):
        return JsonResponse(data)
    return JsonResponse(data)


def do_pull_back_data(from_date, to_date):
    """Pull attendance from all ZK devices for a given date range and save to LiveAttendance."""
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
            saved = 0
            skipped = 0
            for att in attendances:
                att_date = att.timestamp.date()
                if not (from_date <= att_date <= to_date):
                    continue
                try:
                    ts = att.timestamp
                    att_dt = timezone.make_aware(
                        datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second)
                    )
                    player = get_player_info(att.user_id) or {}
                    api_user_id = player.get('userId') or att.user_id
                    window_start = att_dt - timedelta(seconds=60)
                    already_exists = LiveAttendance.objects.filter(
                        user_id=api_user_id,
                        device_ip=d.DeviceIp,
                        att_datetime__gte=window_start,
                        att_datetime__lte=att_dt,
                    ).exists()
                    if not already_exists:
                        rec, created = LiveAttendance.objects.get_or_create(
                            user_id      =api_user_id,
                            att_datetime =att_dt,
                            device_ip    =d.DeviceIp,
                            defaults={
                                'status':      att.status,
                                'device_name': dev_name or '',
                                'full_name':   player.get('fullName') or '',
                                'reg_no':      player.get('registrationNo') or '',
                                'squad':       player.get('squadName') or '',
                                'slot':        player.get('slot') or player.get('slotName') or '',
                            }
                        )
                        if created:
                            saved += 1
                            api_st, api_rs = push_attendance_to_local_api(
                                api_user_id, att_dt, att.status,
                                dev_name or '', d.DeviceIp
                            )
                            rec.api_status = api_st
                            rec.api_reason = api_rs
                            rec.save(update_fields=['api_status', 'api_reason'])
                        else:
                            skipped += 1
                    else:
                        skipped += 1
                except Exception:
                    pass
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

    qs = LiveAttendance.objects.filter(
        att_datetime__date__gte=from_date,
        att_datetime__date__lte=to_date,
    )
    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(full_name__icontains=search) |
            Q(reg_no__icontains=search)    |
            Q(squad__icontains=search)     |
            Q(user_id__icontains=search)
        )

    records = []
    for row in qs:
        records.append({
            'user_id':      row.user_id,
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
    })


def api_today_records(request):
    """Return today's LiveAttendance records as JSON for the auto-attendance table."""
    today = datetime.now().date()
    status_map = {0: 'Check-In', 1: 'Check-Out', 2: 'Break-Out', 3: 'Break-In', 4: 'OT-In', 5: 'OT-Out'}

    qs = LiveAttendance.objects.filter(att_datetime__date=today).order_by('-att_datetime')[:500]
    records = []
    for r in qs:
        records.append({
            'user_id':     r.user_id,
            'full_name':   r.full_name or f'ID:{r.user_id}',
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
            json={'login': 'admin@sfa.pk', 'password': 'F0rward@3456'},
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
                'user_id':    r.user_id,
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

                            for attendance in attendances:
                                if start_date <= attendance.timestamp:
                                    try:
                                        ts = attendance.timestamp
                                        att_dt = timezone.make_aware(
                                            datetime(ts.year, ts.month, ts.day,
                                                     ts.hour, ts.minute, ts.second)
                                        )
                                        player = get_player_info(attendance.user_id) or {}
                                        _, created = LiveAttendance.objects.get_or_create(
                                            user_id      = player.get('userId') or attendance.user_id,
                                            att_datetime = att_dt,
                                            device_ip    = DeviceIp or '',
                                            defaults={
                                                'status':      attendance.status,
                                                'device_name': DeviceName or '',
                                                'full_name':   player.get('fullName') or '',
                                                'reg_no':      player.get('registrationNo') or '',
                                                'squad':       player.get('squadName') or '',
                                                'slot':        player.get('slot') or player.get('slotName') or '',
                                            }
                                        )
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
