# (C) Datadog, Inc. 2018
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)

import logging

from six import iteritems

from datadog_checks.base import ensure_bytes, ensure_unicode
from datadog_checks.checks import AgentCheck

from . import connection, errors, metrics
from .config import IBMMQConfig

try:
    import pymqi
except ImportError as e:
    pymqiException = e
    pymqi = None
else:
    # Since pymqi is not be available/installed on win/macOS when running e2e,
    # we load the following constants only pymqi import succeed
    SUPPORTED_QUEUE_TYPES = [pymqi.CMQC.MQQT_LOCAL, pymqi.CMQC.MQQT_MODEL]

    STATUS_MQCHS_UNKNOWN = -1
    CHANNEL_STATUS_NAME_MAPPING = {
        pymqi.CMQCFC.MQCHS_INACTIVE: "inactive",
        pymqi.CMQCFC.MQCHS_BINDING: "binding",
        pymqi.CMQCFC.MQCHS_STARTING: "starting",
        pymqi.CMQCFC.MQCHS_RUNNING: "running",
        pymqi.CMQCFC.MQCHS_STOPPING: "stopping",
        pymqi.CMQCFC.MQCHS_RETRYING: "retrying",
        pymqi.CMQCFC.MQCHS_STOPPED: "stopped",
        pymqi.CMQCFC.MQCHS_REQUESTING: "requesting",
        pymqi.CMQCFC.MQCHS_PAUSED: "paused",
        pymqi.CMQCFC.MQCHS_INITIALIZING: "initializing",
        STATUS_MQCHS_UNKNOWN: "unknown",
    }

log = logging.getLogger(__file__)


class IbmMqCheck(AgentCheck):

    METRIC_PREFIX = 'ibm_mq'

    SERVICE_CHECK = 'ibm_mq.can_connect'

    QUEUE_MANAGER_SERVICE_CHECK = 'ibm_mq.queue_manager'
    QUEUE_SERVICE_CHECK = 'ibm_mq.queue'

    CHANNEL_SERVICE_CHECK = 'ibm_mq.channel'
    CHANNEL_STATUS_SERVICE_CHECK = 'ibm_mq.channel.status'

    CHANNEL_COUNT_CHECK = 'ibm_mq.channel.count'

    def check(self, instance):
        config = IBMMQConfig(instance)
        config.check_properly_configured()

        if not pymqi:
            log.error("You need to install pymqi: {}".format(pymqiException))
            raise errors.PymqiException("You need to install pymqi: {}".format(pymqiException))

        try:
            queue_manager = connection.get_queue_manager_connection(config)
            self.service_check(self.SERVICE_CHECK, AgentCheck.OK, config.tags)
        except Exception as e:
            self.warning("cannot connect to queue manager: {}".format(e))
            self.service_check(self.SERVICE_CHECK, AgentCheck.CRITICAL, config.tags)
            return

        self.get_pcf_channel_metrics(queue_manager, config.tags_no_channel, config)

        self.discover_queues(queue_manager, config)

        try:
            self.queue_manager_stats(queue_manager, config.tags)

            for queue_name in config.queues:
                queue_tags = config.tags + ["queue:{}".format(queue_name)]

                for regex, q_tags in config.queue_tag_re:
                    if regex.match(queue_name):
                        queue_tags.extend(q_tags)

                try:
                    self.queue_stats(queue_manager, queue_name, queue_tags)
                    # some system queues don't have PCF metrics
                    # so we don't collect those metrics from those queues
                    if queue_name not in config.DISALLOWED_QUEUES:
                        self.get_pcf_queue_metrics(queue_manager, queue_name, queue_tags)
                    self.service_check(self.QUEUE_SERVICE_CHECK, AgentCheck.OK, queue_tags)
                except Exception as e:
                    self.warning('Cannot connect to queue {}: {}'.format(queue_name, e))
                    self.service_check(self.QUEUE_SERVICE_CHECK, AgentCheck.CRITICAL, queue_tags)
        finally:
            queue_manager.disconnect()

    def discover_queues(self, queue_manager, config):
        queues = []
        if config.auto_discover_queues:
            queues.extend(self._discover_queues(queue_manager, '*'))

        if config.queue_patterns:
            for pattern in config.queue_patterns:
                queues.extend(self._discover_queues(queue_manager, pattern))

        if config.queue_regex:
            if not queues:
                queues = self._discover_queues(queue_manager, '*')
            keep_queues = []
            for queue_pattern in config.queue_regex:
                for queue in queues:
                    if queue_pattern.match(queue):
                        keep_queues.append(queue)
            queues = keep_queues

        config.add_queues(queues)

    def _discover_queues(self, queue_manager, mq_pattern_filter):
        queues = []

        for queue_type in SUPPORTED_QUEUE_TYPES:
            args = {pymqi.CMQC.MQCA_Q_NAME: ensure_bytes(mq_pattern_filter), pymqi.CMQC.MQIA_Q_TYPE: queue_type}
            try:
                pcf = pymqi.PCFExecute(queue_manager)
                response = pcf.MQCMD_INQUIRE_Q(args)
            except pymqi.MQMIError as e:
                self.warning("Error discovering queue: {}".format(e))
            else:
                for queue_info in response:
                    queue = queue_info[pymqi.CMQC.MQCA_Q_NAME]
                    queues.append(ensure_unicode(queue).strip())

        return queues

    def queue_manager_stats(self, queue_manager, tags):
        """
        Get stats from the queue manager
        """
        for mname, pymqi_value in iteritems(metrics.queue_manager_metrics()):
            try:
                m = queue_manager.inquire(pymqi_value)
                mname = '{}.queue_manager.{}'.format(self.METRIC_PREFIX, mname)
                self.gauge(mname, m, tags=tags)
                self.service_check(self.QUEUE_MANAGER_SERVICE_CHECK, AgentCheck.OK, tags)
            except pymqi.Error as e:
                self.warning("Error getting queue manager stats: {}".format(e))
                self.service_check(self.QUEUE_MANAGER_SERVICE_CHECK, AgentCheck.CRITICAL, tags)

    def queue_stats(self, queue_manager, queue_name, tags):
        """
        Grab stats from queues
        """
        try:
            args = {pymqi.CMQC.MQCA_Q_NAME: ensure_bytes(queue_name), pymqi.CMQC.MQIA_Q_TYPE: pymqi.CMQC.MQQT_ALL}
            pcf = pymqi.PCFExecute(queue_manager)
            response = pcf.MQCMD_INQUIRE_Q(args)
        except pymqi.MQMIError as e:
            self.warning("Error getting queue stats for {}: {}".format(queue_name, e))
        else:
            # Response is a list. It likely has only one member in it.
            for queue_info in response:
                self._submit_queue_stats(queue_info, queue_name, tags)

    def _submit_queue_stats(self, queue_info, queue_name, tags):
        for metric_suffix, mq_attr in iteritems(metrics.queue_metrics()):
            metric_name = '{}.queue.{}'.format(self.METRIC_PREFIX, metric_suffix)
            if callable(mq_attr):
                metric_value = mq_attr(queue_info)
                if metric_value is not None:
                    self.gauge(metric_name, metric_value, tags=tags)
                else:
                    self.log.debug("Date for attribute %s not found for queue %s", metric_suffix, queue_name)
            else:
                if mq_attr in queue_info:
                    metric_value = queue_info[mq_attr]
                    self.gauge(metric_name, metric_value, tags=tags)
                else:
                    self.log.debug("Attribute %s (%s) not found for queue %s", metric_suffix, mq_attr, queue_name)

    def get_pcf_queue_metrics(self, queue_manager, queue_name, tags):
        try:
            args = {
                pymqi.CMQC.MQCA_Q_NAME: ensure_bytes(queue_name),
                pymqi.CMQC.MQIA_Q_TYPE: pymqi.CMQC.MQQT_ALL,
                pymqi.CMQCFC.MQIACF_Q_STATUS_ATTRS: pymqi.CMQCFC.MQIACF_ALL,
            }
            pcf = pymqi.PCFExecute(queue_manager)
            response = pcf.MQCMD_INQUIRE_Q_STATUS(args)
        except pymqi.MQMIError as e:
            self.warning("Error getting pcf queue stats for {}: {}".format(queue_name, e))
        else:
            # Response is a list. It likely has only one member in it.
            for queue_info in response:
                for mname, values in iteritems(metrics.pcf_metrics()):
                    failure_value = values['failure']
                    pymqi_value = values['pymqi_value']
                    mname = '{}.queue.{}'.format(self.METRIC_PREFIX, mname)
                    m = int(queue_info[pymqi_value])

                    if m > failure_value:
                        self.gauge(mname, m, tags=tags)
                    else:
                        msg = "Unable to get {}, turn on queue level monitoring to access these metrics for {}"
                        msg = msg.format(mname, queue_name)
                        log.debug(msg)

    def get_pcf_channel_metrics(self, queue_manager, tags, config):
        args = {pymqi.CMQCFC.MQCACH_CHANNEL_NAME: ensure_bytes('*')}

        try:
            pcf = pymqi.PCFExecute(queue_manager)
            response = pcf.MQCMD_INQUIRE_CHANNEL(args)
        except pymqi.MQMIError as e:
            self.log.warning("Error getting CHANNEL stats {}".format(e))
        else:
            channels = len(response)
            mname = '{}.channel.channels'.format(self.METRIC_PREFIX)
            self.gauge(mname, channels, tags=tags)

        # Check specific channels
        # If a channel is not discoverable, a user may want to check it specifically.
        # Specific channels are checked first to send channel metrics and `ibm_mq.channel` service checks
        # at the same time, but the end result is the same in any order.
        for channel in config.channels:
            self._submit_channel_status(queue_manager, channel, tags, config)

        # Grab all the discoverable channels
        self._submit_channel_status(queue_manager, '*', tags, config, config.channels)

    def _submit_channel_status(self, queue_manager, search_channel_name, tags, config, channels_to_skip=None):
        """Submit channel status

        Note: Error 3065 (MQRCCF_CHL_STATUS_NOT_FOUND) might indicate that the channel has not been used.
        More info: https://www.ibm.com/support/knowledgecenter/SSFKSJ_7.1.0/com.ibm.mq.doc/fm16690_.htm

        :param search_channel_name might contain wildcard characters
        """
        channels_to_skip = channels_to_skip or []
        search_channel_tags = tags + ["channel:{}".format(search_channel_name)]
        try:
            args = {pymqi.CMQCFC.MQCACH_CHANNEL_NAME: ensure_bytes(search_channel_name)}
            pcf = pymqi.PCFExecute(queue_manager)
            response = pcf.MQCMD_INQUIRE_CHANNEL_STATUS(args)
            self.service_check(self.CHANNEL_SERVICE_CHECK, AgentCheck.OK, search_channel_tags)
        except pymqi.MQMIError as e:
            self.service_check(self.CHANNEL_SERVICE_CHECK, AgentCheck.CRITICAL, search_channel_tags)
            if e.comp == pymqi.CMQC.MQCC_FAILED and e.reason == pymqi.CMQCFC.MQRCCF_CHL_STATUS_NOT_FOUND:
                self.log.debug("Channel status not found for channel {}: {}".format(search_channel_name, e))
            else:
                self.log.warning("Error getting CHANNEL status for channel {}: {}".format(search_channel_name, e))
        else:
            for channel_info in response:
                channel_name = ensure_unicode(channel_info[pymqi.CMQCFC.MQCACH_CHANNEL_NAME]).strip()
                if channel_name in channels_to_skip:
                    continue
                channel_tags = tags + ["channel:{}".format(channel_name)]

                channel_status = channel_info[pymqi.CMQCFC.MQIACH_CHANNEL_STATUS]

                self._submit_channel_count(channel_name, channel_status, channel_tags)
                self._submit_status_check(channel_name, channel_status, channel_tags, config)

    def _submit_status_check(self, channel_name, channel_status, channel_tags, config):
        if channel_status in config.channel_status_mapping:
            service_check_status = config.channel_status_mapping[channel_status]
        else:
            self.log.warning("Status `{}` not found for channel `{}`".format(channel_status, channel_name))
            service_check_status = AgentCheck.UNKNOWN
        self.service_check(self.CHANNEL_STATUS_SERVICE_CHECK, service_check_status, channel_tags)

    def _submit_channel_count(self, channel_name, channel_status, channel_tags):
        if channel_status not in CHANNEL_STATUS_NAME_MAPPING:
            self.log.warning("Status `{}` not found for channel `{}`".format(channel_status, channel_name))
            channel_status = STATUS_MQCHS_UNKNOWN

        for status, status_label in iteritems(CHANNEL_STATUS_NAME_MAPPING):
            status_active = int(status == channel_status)
            self.gauge(self.CHANNEL_COUNT_CHECK, status_active, tags=channel_tags + ["status:" + status_label])
