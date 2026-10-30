import io
from datetime import datetime, timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone
from zk.attendance import Attendance

from . import views
from .models import LiveAttendance, PayRoll_payroll_deviceinfo

PLAYER = {'fullName': 'Real Player', 'userId': 900, 'registrationNo': 'R1', 'squadName': 'U12'}


class AttendanceTestCase(TestCase):
    def setUp(self):
        # Would close the connection that holds TestCase's open transaction
        patcher = mock.patch.object(views, 'close_old_connections')
        patcher.start()
        self.addCleanup(patcher.stop)


def make_record(user_id=501, api_status='failed', full_name='Player', att_datetime=None, zk_user_id=''):
    return LiveAttendance.objects.create(
        user_id=user_id,
        zk_user_id=zk_user_id,
        att_datetime=att_datetime or timezone.now() - timedelta(hours=1),
        device_ip='10.0.0.1',
        full_name=full_name,
        api_status=api_status,
    )


def make_waiting_scan(zk_user_id='105', att_datetime=None):
    return make_record(user_id=None, api_status='pending', full_name='',
                       zk_user_id=zk_user_id, att_datetime=att_datetime)


class RetryUnsentPushesTests(AttendanceTestCase):
    def retry(self, result=('success', 'ok')):
        with mock.patch.object(views, 'push_attendance_to_local_api', return_value=result) as push:
            views.retry_unsent_pushes()
        return push

    def test_failed_record_is_retried_and_marked_success(self):
        rec = make_record(api_status='failed')
        self.retry()
        rec.refresh_from_db()
        self.assertEqual(rec.api_status, 'success')

    def test_old_records_are_retried_whatever_their_age(self):
        old = timezone.now() - timedelta(days=400)
        make_record(user_id=1, api_status='pending', att_datetime=old)
        make_record(user_id=2, api_status='failed', att_datetime=old)
        self.assertEqual(self.retry().call_count, 2)
        self.assertEqual(LiveAttendance.objects.filter(api_status='success').count(), 2)

    def test_finished_records_are_skipped(self):
        for user_id, status in enumerate(['success', 'rejected', 'duplicate'], start=1):
            make_record(user_id=user_id, api_status=status)
        self.assertEqual(self.retry().call_count, 0)

    def test_network_error_stops_the_pass(self):
        make_record(user_id=1)
        make_record(user_id=2)
        push = self.retry(('failed', 'Network error: timed out'))
        self.assertEqual(push.call_count, 1)

    def test_http_error_does_not_stop_the_pass(self):
        make_record(user_id=1)
        make_record(user_id=2)
        push = self.retry(('failed', 'HTTP 500: boom'))
        self.assertEqual(push.call_count, 2)

    def test_oldest_record_is_pushed_first(self):
        now = timezone.now()
        make_record(user_id=2, att_datetime=now - timedelta(hours=1))
        make_record(user_id=1, att_datetime=now - timedelta(hours=2))
        push = self.retry()
        self.assertEqual([c.args[0] for c in push.call_args_list], [1, 2])


class LateRegistrationTests(AttendanceTestCase):
    """A scan whose player is unknown waits, then goes out once the website knows them."""

    def retry(self, player):
        with mock.patch.object(views, 'get_player_info', return_value=player) as lookup, \
                mock.patch.object(views, 'push_attendance_to_local_api',
                                  return_value=('success', 'ok')) as push:
            views.retry_unsent_pushes()
        return push, lookup

    def test_unregistered_player_stays_pending_and_is_not_pushed(self):
        rec = make_waiting_scan()
        push, lookup = self.retry(None)
        self.assertEqual(push.call_count, 0)
        lookup.assert_called_with('105')
        rec.refresh_from_db()
        self.assertEqual((rec.user_id, rec.api_status), (None, 'pending'))

    def test_player_without_a_name_stays_pending(self):
        make_waiting_scan()
        self.assertEqual(self.retry({'fullName': '', 'userId': 900})[0].call_count, 0)

    def test_player_without_a_website_id_is_never_pushed_with_the_zk_number(self):
        make_waiting_scan()
        self.assertEqual(self.retry({'fullName': 'Real Player'})[0].call_count, 0)

    def test_all_waiting_scans_go_out_under_the_real_id_once_registered(self):
        first = timezone.now() - timedelta(days=30)
        second = timezone.now() - timedelta(days=2)
        make_waiting_scan(att_datetime=first)
        make_waiting_scan(att_datetime=second)

        push, _ = self.retry(PLAYER)

        self.assertEqual([c.args[0] for c in push.call_args_list], [900, 900])
        self.assertEqual([c.args[1] for c in push.call_args_list], [first, second])
        for rec in LiveAttendance.objects.all():
            self.assertEqual((rec.user_id, rec.full_name, rec.squad, rec.api_status, rec.zk_user_id),
                             (900, 'Real Player', 'U12', 'success', '105'))

    def test_one_lookup_per_zk_id_however_many_scans_wait(self):
        now = timezone.now()
        for hours in range(1, 6):
            make_waiting_scan(att_datetime=now - timedelta(hours=hours))
        self.assertEqual(self.retry(None)[1].call_count, 1)

    def test_player_api_outage_ends_lookups_for_the_pass(self):
        make_waiting_scan(zk_user_id='105')
        make_waiting_scan(zk_user_id='106')
        with mock.patch.object(views, '_player_api_down', True):
            self.assertEqual(self.retry(None)[1].call_count, 1)

    def test_scan_already_saved_under_the_real_id_is_not_pushed_twice(self):
        when = timezone.now() - timedelta(hours=1)
        rec = make_waiting_scan(att_datetime=when)
        make_record(user_id=900, api_status='success', att_datetime=when)
        self.assertEqual(self.retry(PLAYER)[0].call_count, 0)
        rec.refresh_from_db()
        self.assertEqual(rec.api_status, 'duplicate')


class SaveScanTests(AttendanceTestCase):
    when = datetime(2026, 10, 30, 8, 15, 0)

    def save(self, player, zk_id='105', when=None):
        with mock.patch.object(views, 'get_player_info', return_value=player):
            return views.save_scan(Attendance(zk_id, when or self.when, 0), '10.0.0.1', 'Gate')

    def test_unregistered_player_is_saved_pending_with_original_time(self):
        rec, created = self.save(None)
        self.assertTrue(created)
        self.assertEqual((rec.user_id, rec.zk_user_id, rec.api_status), (None, '105', 'pending'))
        self.assertEqual(rec.att_datetime, timezone.make_aware(self.when))

    def test_non_numeric_zk_id_is_saved(self):
        rec, created = self.save(None, zk_id='A-105')
        self.assertTrue(created)
        self.assertEqual(rec.zk_user_id, 'A-105')

    def test_same_scan_read_twice_is_one_row(self):
        self.save(None)
        _, created = self.save(None)
        self.assertFalse(created)
        self.assertEqual(LiveAttendance.objects.count(), 1)

    def test_same_scan_read_again_after_registration_is_one_row(self):
        self.save(None)
        _, created = self.save(PLAYER)
        self.assertFalse(created)
        self.assertEqual(LiveAttendance.objects.count(), 1)

    def test_scan_matches_a_row_saved_before_zk_user_id_existed(self):
        make_record(user_id=900, api_status='success', att_datetime=timezone.make_aware(self.when))
        _, created = self.save(PLAYER)
        self.assertFalse(created)

    def test_double_tap_is_kept_but_not_pushed(self):
        self.save(None)
        rec, created = self.save(None, when=self.when + timedelta(seconds=30))
        self.assertTrue(created)
        self.assertEqual(rec.api_status, 'duplicate')

    def test_database_refuses_a_second_copy_of_a_scan(self):
        when = timezone.make_aware(self.when)
        make_waiting_scan(att_datetime=when)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_waiting_scan(att_datetime=when)


class AutoPullTests(AttendanceTestCase):
    def setUp(self):
        super().setUp()
        views._device_reads.clear()
        self.addCleanup(views._device_reads.clear)
        PayRoll_payroll_deviceinfo.objects.create(DeviceName='Gate', DeviceIp='10.0.0.1')

    def pull(self, attendances, player, records=None):
        self.conn = mock.Mock(records=len(attendances) if records is None else records)
        self.conn.get_device_name.return_value = 'Gate'
        self.conn.get_attendance.return_value = attendances
        with mock.patch.object(views, 'ZK') as zk, \
                mock.patch.object(views, 'get_player_info', return_value=player), \
                mock.patch.object(views, 'push_attendance_to_local_api',
                                  return_value=('success', 'ok')) as push:
            zk.return_value.connect.return_value = conn = self.conn
            views.do_pull_from_devices()
        return push

    def test_log_is_not_downloaded_when_the_device_count_is_unchanged(self):
        scans = [Attendance('7', datetime.now(), 0)]
        self.pull(scans, PLAYER)
        self.pull(scans, PLAYER)
        self.assertEqual(self.conn.get_attendance.call_count, 0)

    def test_log_is_downloaded_when_the_device_count_changes(self):
        scans = [Attendance('7', datetime.now(), 0)]
        self.pull(scans, PLAYER)
        self.pull(scans, PLAYER, records=2)
        self.assertEqual(self.conn.get_attendance.call_count, 1)

    def test_full_device_with_a_steady_count_is_still_read_hourly(self):
        scans = [Attendance('7', datetime.now(), 0)]
        self.pull(scans, PLAYER)
        views._device_reads['10.0.0.1']['ts'] -= views._DEVICE_FULL_READ_SECONDS + 1
        self.pull(scans, PLAYER)
        self.assertEqual(self.conn.get_attendance.call_count, 1)

    def test_failed_save_forces_the_next_pull_to_read_again(self):
        scans = [Attendance('7', datetime.now(), 0)]
        with mock.patch.object(views, 'save_scan', side_effect=ValueError('boom')):
            self.pull(scans, PLAYER)
        self.pull(scans, PLAYER)
        self.assertEqual(self.conn.get_attendance.call_count, 1)
        self.assertEqual(LiveAttendance.objects.count(), 1)

    def test_scans_already_saved_do_not_touch_save_scan(self):
        scans = [Attendance('7', datetime.now().replace(microsecond=0), 0)]
        self.pull(scans, PLAYER)
        views._device_reads.clear()
        with mock.patch.object(views, 'save_scan') as save:
            self.pull(scans, PLAYER)
        self.assertEqual(save.call_count, 0)

    def test_second_pull_during_a_running_pull_returns_last_result(self):
        with views._pull_lock:
            self.assertIs(views.do_pull_from_devices(), views._last_pull)
        self.assertFalse(LiveAttendance.objects.exists())

    def test_page_request_never_reads_devices(self):
        with mock.patch.object(views, 'ZK') as zk:
            response = self.client.get('/api/auto-pull/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(zk.call_count, 0)

    def test_recent_scan_missed_while_server_was_off_is_saved_and_pushed(self):
        scan = datetime.now() - timedelta(days=views._PULL_LOOKBACK_DAYS - 1)
        push = self.pull([Attendance('7', scan, 0)], PLAYER)
        self.assertEqual(push.call_args.args[0], 900)
        self.assertEqual(LiveAttendance.objects.get().api_status, 'success')

    def test_scan_older_than_lookback_is_ignored(self):
        scan = datetime.now() - timedelta(days=views._PULL_LOOKBACK_DAYS + 2)
        self.pull([Attendance('7', scan, 0)], PLAYER)
        self.assertFalse(LiveAttendance.objects.exists())

    def test_unknown_player_is_saved_pending_without_pushing_the_zk_number(self):
        push = self.pull([Attendance('7', datetime.now(), 0)], None)
        self.assertEqual(push.call_count, 0)
        rec = LiveAttendance.objects.get()
        self.assertEqual((rec.user_id, rec.zk_user_id, rec.api_status), (None, '7', 'pending'))


class PlayerCacheTests(AttendanceTestCase):
    def test_failed_lookup_is_asked_again_after_miss_ttl(self):
        views._player_cache.clear()
        session = mock.Mock()
        session.get.side_effect = ConnectionError('down')
        with mock.patch.object(views, '_http_session', session):
            self.assertIsNone(views.get_player_info('7'))
            views._player_cache['7']['ts'] -= views._PLAYER_MISS_TTL + 1
            session.get.side_effect = None
            session.get.return_value = mock.Mock(status_code=200, json=lambda: {'fullName': 'OK'})
            self.assertEqual(views.get_player_info('7'), {'fullName': 'OK'})
        views._player_cache.clear()


class BackDataPageTests(AttendanceTestCase):
    day = datetime(2026, 10, 29, 16, 0, 0)

    def setUp(self):
        super().setUp()
        when = timezone.make_aware(self.day)
        make_record(user_id=1, api_status='success', att_datetime=when)
        make_record(user_id=2, api_status='duplicate', att_datetime=when)
        make_record(user_id=3, api_status='failed', att_datetime=when)
        make_waiting_scan(zk_user_id='612', att_datetime=when)
        make_waiting_scan(zk_user_id='612', att_datetime=when - timedelta(days=20))
        make_waiting_scan(zk_user_id='618', att_datetime=when - timedelta(days=5))

    def page(self, **params):
        query = {'from_date': '2026-10-29', 'to_date': '2026-10-29', **params}
        request = RequestFactory().get('/back-data/', query)
        with mock.patch.object(views, 'render', wraps=views.render) as render:
            response = views.back_data_page(request)
        response.context = render.call_args.args[2]
        return response

    def counts(self, response):
        return {tab['key']: tab['count'] for tab in response.context['api_tabs']}

    def test_tabs_count_the_selected_dates(self):
        self.assertEqual(self.counts(self.page()),
                         {'all': 4, 'success': 1, 'pending': 1, 'failed': 1, 'duplicate': 1})

    def test_filter_lists_only_that_outcome(self):
        rows = self.page(api='duplicate').context['records']
        self.assertEqual([r['api_status'] for r in rows], ['duplicate'])

    def test_filter_keeps_page_stats_for_the_whole_range(self):
        self.assertEqual(self.page(api='duplicate').context['stats']['total'], 4)

    def test_unsent_view_covers_every_date_and_matches_send_now(self):
        response = self.page(unsent='1')
        self.assertEqual(len(response.context['records']), views.send_status()['unsent'])
        self.assertEqual({r['api_status'] for r in response.context['records']}, {'pending', 'failed'})

    def test_unsent_view_groups_waiting_scans_by_zk_id(self):
        response = self.page(unsent='1')
        self.assertEqual([(p['zk_user_id'], p['scans']) for p in response.context['waiting_players']],
                         [('612', 2), ('618', 1)])
        self.assertEqual(response.context['waiting_scans'], 3)

    def test_unsent_view_explains_what_to_do(self):
        self.assertContains(self.page(unsent='1'), '2 players to register on the website')

    def test_tabs_keep_the_date_range(self):
        self.assertIn('from_date=2026-10-29', self.page().context['api_tabs'][2]['url'])

    def test_time_is_local_not_utc(self):
        self.assertIn('04:00:00 PM', [r['time_only'] for r in self.page().context['records']])

    def test_face_scan_counts_as_check_in(self):
        LiveAttendance.objects.update(status=15)
        self.assertEqual(self.page().context['stats']['check_in'], 4)

    def test_send_bar_links_to_the_unsent_view(self):
        self.assertContains(self.page(), '/back-data/?unsent=1')


class SendNowTests(AttendanceTestCase):
    def post(self):
        request = RequestFactory().post('/api/send-pending/')
        with mock.patch.object(views.threading, 'Thread') as thread:
            response = views.api_send_pending(request)
        return response, thread

    def tearDown(self):
        if views._retry_lock.locked():
            views._retry_lock.release()

    def test_post_starts_a_background_send(self):
        _, thread = self.post()
        thread.return_value.start.assert_called_once_with()

    def test_second_press_while_sending_starts_nothing(self):
        self.post()
        _, thread = self.post()
        self.assertEqual(thread.call_count, 0)

    def test_get_only_reports_progress(self):
        make_record(api_status='failed')
        with mock.patch.object(views.threading, 'Thread') as thread:
            response = views.api_send_pending(RequestFactory().get('/api/send-pending/'))
        self.assertEqual(thread.call_count, 0)
        self.assertIn(b'"unsent": 1', response.content)

    def test_loop_skips_while_the_button_is_sending(self):
        make_record()
        self.post()
        with mock.patch.object(views, 'push_attendance_to_local_api') as push:
            views.retry_unsent_pushes()
        self.assertEqual(push.call_count, 0)

    def test_summary_counts_each_result(self):
        make_record(user_id=1)
        make_record(user_id=2)
        make_waiting_scan()
        results = [('success', 'ok'), ('rejected', 'HTTP 422: no')]
        with mock.patch.object(views, 'get_player_info', return_value=None), \
                mock.patch.object(views, 'push_attendance_to_local_api', side_effect=results):
            views.retry_unsent_pushes()
        status = views.send_status()
        self.assertEqual((status['sent'], status['rejected'], status['waiting'], status['unsent']), (1, 1, 1, 1))
        self.assertFalse(status['running'])

    def test_unreachable_api_is_reported(self):
        make_record()
        with mock.patch.object(views, 'push_attendance_to_local_api',
                               return_value=('failed', 'Network error: timed out')):
            views.retry_unsent_pushes()
        self.assertIn('Network error', views.send_status()['problem'])

    def test_crash_in_a_pass_keeps_the_loop_alive_and_frees_the_lock(self):
        with mock.patch.object(views, '_retry_pass', side_effect=ValueError('boom')):
            views.retry_unsent_pushes()
        self.assertFalse(views._retry_lock.locked())
        self.assertEqual(views.send_status()['problem'], 'boom')


class LogSafetyTests(TestCase):
    def test_print_cannot_stop_a_push_on_a_cp1252_log(self):
        raw = io.BytesIO()
        log = io.TextIOWrapper(raw, encoding='cp1252', write_through=True)
        log.reconfigure(errors='backslashreplace')
        with mock.patch.object(views, '_http_session') as session, \
                mock.patch.object(views, '_local_api_token', 'token'), \
                mock.patch('sys.stdout', log):
            session.put.return_value = mock.Mock(status_code=200, text='ok')
            status, _ = views.push_attendance_to_local_api(7, timezone.now(), 0)
        self.assertEqual(status, 'success')


class RecordsPageTests(AttendanceTestCase):
    day = datetime(2026, 10, 30, 16, 0, 0)

    def setUp(self):
        super().setUp()
        statuses = ['success', 'success', 'pending', 'failed', 'rejected', 'duplicate']
        for offset, status in enumerate(statuses):
            make_record(user_id=offset + 1, api_status=status, full_name=f'Player {offset}',
                        att_datetime=timezone.make_aware(self.day + timedelta(minutes=offset)))
        LiveAttendance.objects.filter(api_status='rejected').update(api_reason='HTTP 422: Player is inactive')

    def page(self, **params):
        # RequestFactory: the test client's context copy breaks on Django 4.2.6 + Python 3.14
        request = RequestFactory().get('/records/', {'date': '2026-10-30', **params})
        with mock.patch.object(views, 'render', wraps=views.render) as render:
            response = views.attendance_records(request)
        response.context = render.call_args.args[2]
        return response

    def counts(self, response):
        return {tab['key']: tab['count'] for tab in response.context['api_tabs']}

    def test_tabs_count_each_api_outcome(self):
        self.assertEqual(self.counts(self.page()),
                         {'all': 6, 'success': 2, 'pending': 1, 'failed': 2, 'duplicate': 1})

    def test_filter_shows_only_that_outcome_but_keeps_all_counts(self):
        response = self.page(api='failed')
        self.assertEqual({r['api_label'] for r in response.context['records']}, {'Failed', 'Rejected'})
        self.assertEqual(self.counts(response)['all'], 6)

    def test_unknown_filter_falls_back_to_all(self):
        self.assertEqual(len(self.page(api='nonsense').context['records']), 6)

    def test_api_response_text_is_shown(self):
        self.assertContains(self.page(api='failed'), 'HTTP 422: Player is inactive')

    def test_missing_reason_gets_a_readable_message(self):
        self.assertContains(self.page(api='pending'), 'Waiting to send to API')

    def test_tabs_keep_date_and_search(self):
        tab = self.page(search='Player 1').context['api_tabs'][1]
        self.assertIn('date=2026-10-30', tab['url'])
        self.assertIn('search=Player+1', tab['url'])

    def test_time_is_local_not_utc(self):
        rows = self.page().context['records']
        self.assertIn('04:00:00 PM', [r['time_only'] for r in rows])

    def test_sent_percent_ignores_duplicates(self):
        self.assertEqual(self.page().context['stats']['sent_percent'], 40)

    def test_unknown_player_is_labelled(self):
        make_waiting_scan(att_datetime=timezone.make_aware(self.day))
        self.assertContains(self.page(api='pending'), 'Unknown player')


class PushResultTests(AttendanceTestCase):
    def push(self, status_code, att_datetime=None):
        response = mock.Mock(status_code=status_code, text='nope')
        response.json.return_value = {'message': 'nope'}
        session = mock.Mock()
        session.put.return_value = response
        with mock.patch.object(views, '_http_session', session), \
                mock.patch.object(views, '_local_api_token', 'token'), \
                mock.patch.object(views, '_get_local_api_token'):
            result = views.push_attendance_to_local_api(
                7, att_datetime or timezone.now(), 0, 'Gate', '10.0.0.1'
            )
        return result, session

    def test_refused_record_is_rejected_with_the_api_reason(self):
        for code in (400, 404, 422):
            self.assertEqual(self.push(code)[0], ('rejected', f'HTTP {code}: nope'))

    def test_temporary_errors_stay_failed(self):
        for code in (401, 403, 408, 429, 500, 503):
            self.assertEqual(self.push(code)[0][0], 'failed')

    def test_missing_user_id_is_rejected(self):
        status, _ = views.push_attendance_to_local_api(None, timezone.now(), 0)
        self.assertEqual(status, 'rejected')

    def test_date_is_local_for_a_value_read_back_from_the_db(self):
        rec = make_record(att_datetime=timezone.make_aware(datetime(2026, 10, 30, 3, 0, 0)))
        stored = LiveAttendance.objects.values('att_datetime').get(pk=rec.pk)['att_datetime']
        _, session = self.push(200, stored)
        self.assertEqual(session.put.call_args.kwargs['json']['date'], '2026-10-30')
