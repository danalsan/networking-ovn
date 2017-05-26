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

import eventlet

from neutron.agent.common import utils
from oslo_log import log
from ovs.stream import Stream
from ovsdbapp.backend.ovs_idl import connection
from ovsdbapp.backend.ovs_idl import idlutils

from networking_ovn.common import config
from networking_ovn.agent.metadata import server
from networking_ovn.ovsdb import impl_idl_ovn as idl_ovn
from networking_ovn.ovsdb import ovsdb_monitor
from networking_ovn.ovsdb import row_event
from networking_ovn.ovsdb import vlog


LOG = log.getLogger(__name__)
NS_PREFIX = 'qmeta-'


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
        #helper.register_table('Port_Binding')
        #helper.register_table('Chassis')
        #helper.register_table('Datapath')
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
        elif len(old.chassis)  and old.chassis[0].name == self.chassis:
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
    cmd = 'ovs-vsctl get open . external_ids:system-id'
    return utils.execute(cmd.split(),
                         run_as_root=True).strip().replace('"', '')


class MetadataAgent(object):

    def __init__(self, conf):
        self.conf = conf
        vlog.use_oslo_logger()

    def start(self):
        idl_nb = OvnNbIdl.from_server(config.get_ovn_nb_connection(),
                                      'OVN_Northbound', None)
        self.idl_nb_conn = connection.Connection(
            idl_nb, timeout=config.get_ovn_ovsdb_timeout())

        self.chassis = _get_own_chassis_name()
        idl_sb = OvnSbIdl.from_server(config.get_ovn_sb_connection(),
                                      'OVN_Southbound', self.chassis)
        self.idl_sb_conn = connection.Connection(
            idl_sb, timeout=config.get_ovn_ovsdb_timeout())

        # Open the connection to both OVN NB and SB databases.
        self.idl_nb_conn.start()
        self.idl_sb_conn.start()

        import pdb; pdb.set_trace()
        self.ensure_all_networks_provisioned()

        # Launch the server that will act as a proxy between the VM's and Nova.
        proxy = server.UnixDomainMetadataProxy(self.conf)
        proxy.run()

    def sync():
        pass

    def ensure_all_networks_provisioned(self):
        idl = idl_ovn.OvsdbSbOvnIdl(self.idl_sb_conn)
        ports = idl.get_ports_on_chassis(self.chassis)
        datapaths = {str(p.datapath.uuid)  for p in ports if p.type == ''}
        print datapaths


if __name__ == '__mainX__':

    idl_nb = OvnNbIdl.from_server('tcp:127.0.0.1:6641', 'OVN_Northbound', None)
    idl_nb_conn = connection.Connection(idl_nb, timeout=180)
    idl_nb_conn.start()

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
