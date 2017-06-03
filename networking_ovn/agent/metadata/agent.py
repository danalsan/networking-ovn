#            DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
#                    Version 2, December 2004
#
# Copyright (C) 2017 Red Hat, Inc.
#
# Everyone is permitted to copy and distribute verbatim or modified
# copies of this license document, and changing it is allowed as long
# as the name is changed.
#
#            DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
#   TERMS AND CONDITIONS FOR COPYING, DISTRIBUTION AND MODIFICATION
#
#  0. You just DO WHAT THE FUCK YOU WANT TO.

# Copyright 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import eventlet
import re

from neutron.agent.common import ovs_lib
from neutron.agent.common import utils
from neutron.agent.linux import external_process
from neutron.agent.linux import ip_lib
from neutron_lib import constants as n_const
from oslo_log import log
from ovs.stream import Stream
from ovsdbapp.backend.ovs_idl import connection
from ovsdbapp.backend.ovs_idl import idlutils

from networking_ovn.common import config
from networking_ovn.common import constants as ovn_const
from networking_ovn.agent.metadata import driver as metadata_driver
from networking_ovn.agent.metadata import server
from networking_ovn.ovsdb import impl_idl_ovn as idl_ovn
from networking_ovn.ovsdb import ovsdb_monitor
from networking_ovn.ovsdb import row_event
from networking_ovn.ovsdb import vlog


LOG = log.getLogger(__name__)

NS_PREFIX = 'qmeta-'
METADATA_DEFAULT_PREFIX = 16
METADATA_DEFAULT_IP = '169.254.169.254'
METADATA_DEFAULT_CIDR = '%s/%d' % (METADATA_DEFAULT_IP,
                                   METADATA_DEFAULT_PREFIX)
METADATA_PORT = 80
MAC_PATTERN = re.compile(r'([0-9A-F]{2}[:-]){5}([0-9A-F]{2})', re.I)

MetadataPortInfo = collections.namedtuple('MetadataPortInfo', ['mac',
                                                               'ip_addresses'])

class OvnNbIdl(ovsdb_monitor.OvnIdl):
    def __init__(self, driver, remote, schema):
        super(OvnNbIdl, self).__init__(driver, remote, schema)
        self.event_lock_name = "networking_ovn_metadata_agent"

    @classmethod
    def from_server(cls, connection_string, schema_name, driver):
        _check_and_set_ssl_files(schema_name)
        helper = idlutils.get_schema_helper(connection_string, schema_name)
        helper.register_all()
        _idl = cls(driver, connection_string, helper)
        _idl.set_lock(_idl.event_lock_name)
        return _idl


class OvnSbIdl(ovsdb_monitor.OvnIdl):
    def __init__(self, remote, schema, chassis):
        super(OvnSbIdl, self).__init__(None, remote, schema)
        self._pb_update_event = PortBindingChassisEvent(chassis)
        self.notify_handler.watch_events([self._pb_update_event])
        self.event_lock_name = "networking_ovn_metadata_agent"

    @classmethod
    def from_server(cls, connection_string, schema_name, chassis):
        _check_and_set_ssl_files(schema_name)
        helper = idlutils.get_schema_helper(connection_string, schema_name)
        helper.register_all()
        _idl = cls(connection_string, helper, chassis)
        _idl.set_lock(_idl.event_lock_name)
        return _idl


class PortBindingChassisEvent(row_event.RowEvent):
    def __init__(self, chassis):
        self.chassis = chassis
        table = 'Port_Binding'
        events = (self.ROW_UPDATE)
        super(PortBindingChassisEvent, self).__init__(
            events, table, None)
        self.event_name = 'PortBindingChassisEvent'

    def run(self, event, row, old):
        if len(row.chassis) and row.chassis[0].name == self.chassis:
            print("Port bound to our chassis")
        elif len(old.chassis) and old.chassis[0].name == self.chassis:
            print("Port unbound to our chassis")
        print len(row.chassis)


def _check_and_set_ssl_files(schema_name):
    if schema_name == 'OVN_Southbound':
        priv_key_file = config.get_ovn_sb_private_key()
        cert_file = config.get_ovn_sb_certificate()
        ca_cert_file = config.get_ovn_sb_ca_cert()
    else:
        priv_key_file = config.get_ovn_nb_private_key()
        cert_file = config.get_ovn_nb_certificate()
        ca_cert_file = config.get_ovn_nb_ca_cert()

    if priv_key_file:
        Stream.ssl_set_private_key_file(priv_key_file)

    if cert_file:
        Stream.ssl_set_certificate_file(cert_file)

    if ca_cert_file:
        Stream.ssl_set_ca_cert_file(ca_cert_file)


def _get_own_chassis_name():
    """Return the external_ids:system-id value of the Open_vSwitch table.

    As long as ovn-controller is running on this node, the key is guaranteed
    to exist and will include the chassis name.
    """
    ext_ids = ovs_lib.BaseOVS().db_get_val('Open_vSwitch', '.', 'external_ids')
    return ext_ids['system-id']

class MetadataAgent(object):

    def __init__(self, conf):
        self.conf = conf
        vlog.use_oslo_logger()
        self._process_monitor = external_process.ProcessMonitor(
            config=self.conf,
            resource_type='metadata')

    def start(self):

        self.chassis = _get_own_chassis_name()
        idl_sb = OvnSbIdl.from_server(config.get_ovn_sb_connection(),
                                      'OVN_Southbound', self.chassis)
        self.idl_sb_conn = connection.Connection(
            idl_sb, timeout=config.get_ovn_ovsdb_timeout())

        # Open the connection to OVN SB database.
        self.idl_sb_conn.start()
        self.sb_idl = idl_ovn.OvsdbSbOvnIdl(self.idl_sb_conn)

        self.ensure_all_networks_provisioned()

        # Launch the server that will act as a proxy between the VM's and Nova.
        proxy = server.UnixDomainMetadataProxy(self.conf, self.sb_idl)
        proxy.run()

    def sync():
        pass

    @staticmethod
    def _get_veth_name(datapath):
        return ['{}{}{}'.format(n_const.TAP_DEVICE_PREFIX,
                                 datapath[:10], i) for i in [0, 1]]

    def ensure_all_networks_provisioned(self):
        # 1. Retrieve all datapaths where we have ports bound in our chassis
        # 2. For every datapath make sure that:
        #   2.1 The namespace is created
        #   2.2 The VETH pair is created
        #   2.3 An OVS port exists with the right external-id:iface-id
        #   2.4 metadata proxy is running
        #   2.5 It's added to the Chassis column externa-id

        # 1. Retrieve all ports in our Chassis with type == ''
        ports = self.sb_idl.get_ports_on_chassis(self.chassis)
        datapaths = {str(p.datapath.uuid) for p in ports if p.type == ''}
        for datapath in datapaths:
            port = self.sb_idl.get_metadata_port_network(datapath)
            # If there's no metadata port, it has no MAC address or no IP's,
            # then skip this datapath.
            # TODO(dalvarez): Tear down the namespace if exists when skipping
            # a datapath.
            if not (port and port.mac and
                    port.external_ids.get(ovn_const.OVN_CIDRS_EXT_ID_KEY,
                                          None)):
                continue

            # First entry of the mac field must be the MAC address
            match = MAC_PATTERN.match(port.mac[0].split(' ')[0])
            if not match:
                continue

            mac = match.group()
            ip_addresses = set(
                port.external_ids[ovn_const.OVN_CIDRS_EXT_ID_KEY].split(' '))
            ip_addresses.add(METADATA_DEFAULT_CIDR)
            metadata_port = MetadataPortInfo(mac, ip_addresses)

            # 2.1 Make sure that the namespace is created
            # 2.2 Make sure that the VETH pair exists
            namespace = NS_PREFIX + datapath
            #ip_lib.IPWrapper().ensure_namespace(namespace)
            veth_name = self._get_veth_name(datapath)

            if ip_lib.device_exists(veth_name[1], namespace):
                ip2 = ip_lib.IPDevice(veth_name[1], namespace)
            else:
                ip1, ip2 = ip_lib.IPWrapper().add_veth(
                    veth_name[0], veth_name[1], namespace)
                ip1.link.set_up()
                ip2.link.set_up()

            # Configure the MAC and IP addresses
            ip2.link.set_address(metadata_port.mac)
            dev_info = ip2.addr.list()

            # Configure the IP addresses on the VETH pair and remove those
            # that we no longer need.
            current_cidrs = {dev['cidr'] for dev in dev_info}
            for ipaddr in current_cidrs - metadata_port.ip_addresses:
                ip2.addr.delete(ipaddr)
            for ipaddr in metadata_port.ip_addresses - current_cidrs:
                ip2.addr.add(ipaddr)

            # Configure the OVS port and add external_ids:iface-id so that it
            # can be tracked by OVN.
            # TODO(dalvarez): pick integration bridge name from conf
            ovs_br = ovs_lib.OVSBridge('br-int')
            ovs_br.add_port(veth_name[0])
            ovs_br.set_db_attribute('Interface', veth_name[0], 'external_ids',
                                    {'iface-id': port.logical_port})

            # Spawn metadata proxy
            metadata_driver.MetadataDriver.spawn_monitored_metadata_proxy(
                self._process_monitor, namespace, METADATA_PORT,
                self.conf, network_id=str(port.datapath.uuid))

        #ip1.link.set_address(
        current_dps = self.sb_idl.get_chassis_metadata_networks(self.chassis)
        #with self._nb_ovn.transaction(check_error=True) as txn:
        if datapaths != current_dps:
            with self.sb_idl.transaction(check_error=True) as txn:
                txn.add(self.sb_idl.set_chassis_metadata_networks(
                    self.chassis, datapaths))
        #  datapaths - set(current_dps)
        print current_dps
        print datapaths


if __name__ == '__mainX__':

    idl_nb = OvnNbIdl.from_server('tcp:127.0.0.1:6641', 'OVN_Northbound', None)
    idl_nb_conn = connection.Connection(idl_nb, timeout=180)
    idl_nb_conn.start()

    ovs = ovs_lib.BaseOVS()
    ovs.db_get_val('open', '.', 'external_ids', 'system-id')
    cmd = 'ovs-vsctl get open . external_ids:system-id'
    chassis = utils.execute(cmd.split(),
                            run_as_root=True).strip().replace('"', '')

    LOG.debug('chassis: %s', chassis)
    idl_sb = OvnSbIdl.from_server('tcp:127.0.0.1:6642', 'OVN_Southbound',
                                  chassis)
    idl_sb_conn = connection.Connection(idl_sb, timeout=180)
    idl_sb_conn.start()

    nb_idl = idl_ovn.OvsdbNbOvnIdl(idl_nb_conn)
    import pdb; pdb.set_trace()
    nets = nb_idl.get_all_logical_switches_with_ports()

    while True:
        eventlet.sleep(0)
