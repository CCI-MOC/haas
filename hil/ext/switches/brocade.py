"""A switch driver for Brocade NOS.

Uses the XML REST API for communicating with the switch.
"""

import logging
from lxml import etree
from os.path import dirname, join
import re
import requests
from schema import Schema, Optional

from hil.migrations import paths
from hil.model import db, Switch
from hil.errors import BadArgumentError, SwitchError
from hil.model import BigIntegerType
from hil.ext.switches.common import check_native_networks, parse_vlans
from hil.config import core_schema, string_is_bool
from hil.ext.switches import _vlan_http


paths[__name__] = join(dirname(__file__), 'migrations', 'brocade')

logger = logging.getLogger(__name__)
core_schema[__name__] = {
    Optional('save'): string_is_bool
}


class Brocade(Switch, _vlan_http.Session):
    """Brocade switch"""

    api_name = 'http://schema.massopencloud.org/haas/v0/switches/brocade'

    __mapper_args__ = {
        'polymorphic_identity': api_name,
    }

    id = db.Column(BigIntegerType,
                   db.ForeignKey('switch.id'), primary_key=True)
    hostname = db.Column(db.String, nullable=False)
    username = db.Column(db.String, nullable=False)
    password = db.Column(db.String, nullable=False)
    interface_type = db.Column(db.String, nullable=False)

    @staticmethod
    def validate(kwargs):
        Schema({
            'hostname': basestring,
            'username': basestring,
            'password': basestring,
            'interface_type': basestring,
        }).validate(kwargs)

    def session(self):
        return self

    def ensure_legal_operation(self, nic, op_type, channel):
        check_native_networks(nic, op_type, channel)

    @staticmethod
    def validate_port_name(port):
        """Valid port names for this switch are of the form 1/0/1 or 1/2"""

        val = re.compile(r'^\d+/\d+(/\d+)?$')
        if not val.match(port):
            raise BadArgumentError("Invalid port name. Valid port names for "
                                   "this switch are of the from 1/0/1 or 1/2")
        return

    def get_capabilities(self):
        return []

    def _port_shutdown(self, interface):
        """Shuts down port"""
        payload = '<shutdown>true</shutdown>'
        url = self._construct_url(interface)
        # accepting 409 makes it idempotent
        self._make_request('POST', url, data=payload,
                           acceptable_error_codes=(409,))

    def _is_interface_off(self, interface):
        """ Returns a boolean that tells the status of a switchport"""

        url = self._construct_url(interface)
        response = self._make_request('GET', url)
        root = etree.fromstring(response.text)
        shutdown = root.find(self._construct_tag("shutdown"))
        if shutdown is None:
            return False
        elif shutdown.text == "true":
            return True
        else:
            raise SwitchError("Could not determine if interface is off")

    def _get_mode(self, interface):
        """ Return the mode of an interface.

        Args:
            interface: interface to return the mode of

        Returns: 'access' or 'trunk'

        Raises: AssertionError if mode is invalid.

        """
        url = self._construct_url(interface, suffix='mode')
        response = self._make_request('GET', url)
        root = etree.fromstring(response.text)
        mode = root.find(self._construct_tag('vlan-mode')).text
        return mode

    def _enable_and_set_mode(self, interface, mode):
        """ Enables switching and sets the mode of an interface.

        Args:
            interface: interface to set the mode of
            mode: 'access' or 'trunk'

        Raises: AssertionError if mode is invalid.
        """
        url = self._construct_url(interface)

        # Turn on port; equivalent to running `no shutdown` on the switchport
        if self._is_interface_off(interface):
            self._make_request('DELETE', url+"/shutdown")

        # Enable switching
        payload = '<switchport></switchport>'
        self._make_request('POST', url, data=payload,
                           acceptable_error_codes=(409,))

        # Set the interface mode
        if mode in ['access', 'trunk']:
            url = self._construct_url(interface, suffix='mode')
            payload = '<mode><vlan-mode>%s</vlan-mode></mode>' % mode
            self._make_request('PUT', url, data=payload)
        else:
            raise AssertionError('Invalid mode')

    def _get_vlans(self, interface):
        """ Return the vlans of a trunk port.

        Does not include the native vlan. Use _get_native_vlan.

        Args:
            interface: interface to return the vlans of

        Returns: List containing the vlans of the form:
        [('vlan/vlan1', vlan1), ('vlan/vlan2', vlan2)]
        """
        try:
            url = self._construct_url(interface, suffix='trunk')
            response = self._make_request('GET', url)
            root = etree.fromstring(response.text)
            vlans = root. \
                find(self._construct_tag('allowed')).\
                find(self._construct_tag('vlan')).\
                find(self._construct_tag('add')).text

            # finds a comma separated list of integers and/or ranges.
            # Sample: 12,14-18,23,28,80-90 or 20 or 20,22 or 20-22
            match = re.search(r'(\d+(-\d+)?)(,\d+(-\d+)?)*', vlans)
            if match is None:
                return []

            vlan_list = parse_vlans(match.group())

            return [('vlan/%s' % x, x) for x in vlan_list]
        except AttributeError:
            return []

    def _get_native_vlan(self, interface):
        """ Return the native vlan of an interface.

        Args:
            interface: interface to return the native vlan of

        Returns: Tuple of the form ('vlan/native', vlan) or None
        """
        try:
            url = self._construct_url(interface, suffix='trunk')
            response = self._make_request('GET', url)
            root = etree.fromstring(response.text)
            vlan = root.find(self._construct_tag('native-vlan')).text
            return ('vlan/native', vlan)
        except AttributeError:
            return None

    def _add_vlan_to_trunk(self, interface, vlan):
        """ Add a vlan to a trunk port.

        If the port is not trunked, its mode will be set to trunk.

        Args:
            interface: interface to add the vlan to
            vlan: vlan to add
        """
        self._enable_and_set_mode(interface, 'trunk')
        url = self._construct_url(interface, suffix='trunk/allowed/vlan')
        payload = '<vlan><add>%s</vlan></vlan>' % vlan
        self._make_request('PUT', url, data=payload)

    def _remove_vlan_from_trunk(self, interface, vlan):
        """ Remove a vlan from a trunk port.

        Args:
            interface: interface to remove the vlan from
            vlan: vlan to remove
        """
        url = self._construct_url(interface, suffix='trunk/allowed/vlan')
        payload = '<vlan><remove>%s</remove></vlan>' % vlan
        self._make_request('PUT', url, data=payload)

    def _remove_all_vlans_from_trunk(self, interface):
        """ Remove all vlan from a trunk port.

        Args:
            interface: interface to remove the vlan from
        """
        url = self._construct_url(interface, suffix='trunk/allowed/vlan')
        payload = '<vlan><none>true</none></vlan>'
        requests.put(url, data=payload, auth=self._auth)

    def _set_native_vlan(self, interface, vlan):
        """ Set the native vlan of an interface.

        Args:
            interface: interface to set the native vlan to
            vlan: vlan to set as the native vlan
        """
        self._enable_and_set_mode(interface, 'trunk')
        self._disable_native_tag(interface)
        url = self._construct_url(interface, suffix='trunk')
        payload = '<trunk><native-vlan>%s</native-vlan></trunk>' % vlan
        self._make_request('PUT', url, data=payload)

    def _remove_native_vlan(self, interface):
        """ Remove the native vlan from an interface.

        Args:
            interface: interface to remove the native vlan from
        """
        url = self._construct_url(interface, suffix='trunk/native-vlan')
        self._make_request('DELETE', url)

    def _disable_native_tag(self, interface):
        """ Disable tagging of the native vlan

        Args:
            interface: interface to disable the native vlan tagging of

        """
        url = self._construct_url(interface, suffix='trunk/tag/native-vlan')
        self._make_request('DELETE', url, acceptable_error_codes=(404,))

    def _construct_url(self, interface, suffix=''):
        """ Construct the API url for a specific interface appending suffix.

        Args:
            interface: interface to construct the url for
            suffix: suffix to append at the end of the url

        Returns: string with the url for a specific interface and operation
        """
        # %22 is the encoding for double quotes (") in urls.
        # % escapes the % character.
        # Double quotes are necessary in the url because switch ports contain
        # forward slashes (/), ex. 101/0/10 is encoded as "101/0/10".
        return '%(hostname)s/rest/config/running/interface/' \
            '%(interface_type)s/%%22%(interface)s%%22%(suffix)s' \
            % {
                'hostname': self.hostname,
                'interface_type': self.interface_type,
                'interface': interface,
                'suffix': '/switchport/%s' % suffix if suffix else ''
            }

    @staticmethod
    def _construct_tag(name):
        """ Construct the xml tag by prepending the brocade tag prefix. """
        return '{urn:brocade.com:mgmt:brocade-interface}%s' % name
