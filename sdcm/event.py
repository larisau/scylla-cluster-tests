import os
import datetime
import logging
import time
import signal
from multiprocessing import Process
import requests
import json
from prometheus import SCTMetrics
from avocado.utils import process

logger = logging.getLogger(__name__)

COLOR = {'white':   "\033[0;37;60m",
         'red':     "\033[1;31;60m",
         'green':   "\033[1;32;60m",
         'yellow':  "\033[1;33;60m",
         'magenta': "\033[1;35;60m",
         'cyan':    "\033[1;36;60m",
         'end':     "\033[0m"
         }
EVENT_LOG_FILENAME = 'events.log'
event_log = None


class SCTEvent(object):

    _COLOR = COLOR['white']

    def __init__(self, name='common', msg=''):
        self._name = name.upper()
        self._mgs = msg
        self._timestamp = self._timestamp()
        self._max_name_len = 10

    def _timestamp(self):
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def __str__(self):
        return '%-20s %s%-8s %10s%s %s\n' % (self._timestamp,
                                             self._COLOR,
                                             self.__class__.__name__.upper().lstrip('SCT'),
                                             self._name if len(self._name) <= 10 else self._name[:10],
                                             COLOR['end'],
                                             self._mgs
                                             )


class SCTAlert(SCTEvent):
    _COLOR = COLOR['magenta']


class SCTError(SCTEvent):
    _COLOR = COLOR['red']


class SCTFailure(SCTEvent):
    _COLOR = COLOR['yellow']


class SCTWarning(SCTEvent):
    _COLOR = COLOR['cyan']


class SCTInfo(SCTEvent):
    _COLOR = COLOR['green']


class EventLog(object):

    def __init__(self, file_name):
        self._log_filename = file_name

    def save(self, event):
        if not isinstance(event, SCTEvent):
            event = SCTEvent(msg=event)
        try:
            with open(self._log_filename, 'a') as fd_write:
                fd_write.write(str(event))
        except Exception as ex:
            logger.error('Cannot write to event log: %s', ex)


def get_event_log(log_file_dir='/tmp'):
    global event_log
    if not event_log:
        event_log = EventLog(os.path.join(log_file_dir, EVENT_LOG_FILENAME))
    return event_log


class EventHandler(Process):
    """
    Check error counters on prometheus,
    stop test if number of errors exceeded a threshold.
    This task is running as a detached process during the test.
    """
    CRITICAL_EVENTS = []
    ERROR_EVENTS = [(SCTMetrics.COREDUMP_COUNTER, 'node'),
                    (SCTMetrics.DISRUPT_ERROR_COUNTER, 'method')]
    EXCLUDED_EVENTS = []
    alive = True

    def __init__(self, server_ip):
        """
        :param server_ip: prometheus server ip
        """
        Process.__init__(self)
        self._prometheus_url = 'http://{}:9090/api/v1'.format(server_ip)
        self._test_pid = os.getpid()
        logger.debug('Test pid: %s, ppid: %s', self._test_pid, os.getppid())
        self._sleep_interval = 30
        self._errors_threshold = 10
        self._critical_errors_threshold = 1

    def _query(self, query):
        url = '{}/query?{}'.format(self._prometheus_url, query)
        resp = requests.get(url)
        if resp.status_code not in [200, 201, 202]:
            logger.error('Failed getting metrics, error: %s', resp.content)
        logger.debug('Metrics: %s', resp.content)
        return json.loads(resp.content)

    def _suspend_test(self):
        logger.debug('Suspend test, to resume run: kill -18 %s', self._test_pid)
        if not process.safe_kill(self._test_pid, signal.SIGSTOP):
            logger.error('Cannot suspend test')

    def _terminate_test(self):
        logger.debug('Terminate test due to errors(test pid %s).', self._test_pid)
        if not process.safe_kill(self._test_pid, signal.SIGTERM) or process.pid_exists(self._test_pid):
            process.safe_kill(self._test_pid, signal.SIGKILL)

    def _get_event_count(self, event_list):
        cnt = 0
        for metric in event_list:
            try:
                res = self._query('query={}&?time={}'.format(metric[0], int(time.time())))
                events = {r['metric'][metric[1]]: int(r['value'][1]) for r in res['data']['result']}
                if events:
                    logger.debug('Events found: %s', events)
                    cnt += sum(events.values())
            except Exception as ex:
                logger.error('Failed getting event count: %s', ex)
        return cnt

    def get_critical_events(self):
        return self._get_event_count(self.CRITICAL_EVENTS)

    def get_error_events(self):
        return self._get_event_count(self.ERROR_EVENTS)

    def run(self):
        logger.debug('Start event handler.')
        while self.alive:
            time.sleep(self._sleep_interval)
            if self.get_critical_events() >= self._critical_errors_threshold or\
                    self.get_error_events() >= self._errors_threshold:
                self._terminate_test()
                self.stop()

    @classmethod
    def stop(cls):
        logger.debug('Stop event handler.')
        cls.alive = False


def start_event_handler(server_ip):
    eh = EventHandler(server_ip)
    eh.daemon = True
    eh.start()


def stop_event_handler():
    EventHandler.stop()
