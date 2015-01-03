# -*- coding: utf-8 -*-

__version__ = "1.0"

"""
##--------------------------------------#
## Kvasir
##
## (c) 2010-2014 Cisco Systems, Inc.
## (c) 2015 Kurt Grutzmacher
##
## Nessus File Processor for Kvasir
##
## Author: Kurt Grutzmacher <grutz@jingojango.net>
##--------------------------------------#
"""

from . import nessus_get_config
from .hosts import NessusHosts
from .vulns import NessusVulns
from gluon import current
import sys
import os
import re
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO
from skaldship.hosts import do_host_status
from skaldship.exploits import connect_exploits
from skaldship.services import Services
from skaldship.log import log
import logging

try:
    from lxml import etree
except ImportError:
    import sys
    if not sys.hexversion >= 0x02070000:
        raise Exception('python-lxml or Python 2.7 or higher required for Nessus parsing')
    try:
        from xml.etree import cElementTree as etree
    except ImportError:
        try:
            from xml.etree import ElementTree as etree
        except:
            raise Exception('No valid ElementTree parser found')


##-------------------------------------------------------------------------
def process_scanfile(
    filename=None,
    asset_group=None,
    engineer=None,
    msf_settings={},
    ip_ignore_list=None,
    ip_include_list=None,
    update_hosts=False,
    ):
    """
    Process a Nessus XML or CSV Report file. There are two types of CSV output, the first
    is very basic and is generated by a single Nessus instance. The second comes from the
    centralized manager. I forget what it's called but it packs more data. If you have a
    standalone scanner, always export/save as .nessus.

    :param filename: A local filename to process
    :param asset_group: Asset group to assign hosts to
    :param engineer: Engineer record number to assign hosts to
    :param msf_workspace: If set a Metasploit workspace to send the scanfile to via the API
    :param ip_ignore_list: List of IP addresses to ignore
    :param ip_include_list: List of IP addresses to ONLY import (skip all others)
    :param update_hosts: Boolean to update/append to hosts, otherwise hosts are skipped
    :returns msg: A string status message
    """
    from skaldship.cpe import lookup_cpe
    nessus_config = nessus_get_config()

    db = current.globalenv['db']

    # build the hosts only/exclude list
    ip_exclude = []
    if ip_ignore_list:
        ip_exclude = ip_ignore_list.split('\r\n')
        # TODO: check for ip subnet/range and break it out to individuals
    ip_only = []
    if ip_include_list:
        ip_only = ip_include_list.split('\r\n')
        # TODO: check for ip subnet/range and break it out to individuals

    log(" [*] Processing Nessus scan file %s" % filename)

    fIN = open(filename, "rb")
    # check to see if file is a CSV file, if so set nessus_csv to True
    line = fIN.readline()
    fIN.seek(0)
    if line.startswith('Plugin'):
        import csv
        csv.field_size_limit(sys.maxsize)           # field size must be increased
        nessus_iterator = csv.DictReader(fIN)
        nessus_csv_type = 'Standalone'
        log(" [*] CSV file is from Standalone scanner")
    elif line.startswith('"Plugin"'):
        import csv
        csv.field_size_limit(sys.maxsize)           # field size must be increased
        nessus_iterator = csv.DictReader(fIN)
        nessus_csv_type = 'SecurityCenter'
        log(" [*] CSV file is from SecurityCenter")
    else:
        nessus_csv_type = False
        try:
            nessus_xml = etree.parse(filename)
            log(" [*] XML file identified")
        except etree.ParseError, e:
            msg = " [!] Invalid Nessus scan file (%s): %s " % (filename, e)
            log(msg, logging.ERROR)
            return msg

        root = nessus_xml.getroot()
        nessus_iterator = root.findall("Report/ReportHost")

    nessus_hosts = NessusHosts(engineer, asset_group, ip_include_list, ip_ignore_list, update_hosts)
    nessus_vulns = NessusVulns()
    services = Services()
    svcs = db.t_services

    def _plugin_parse(host_id, vuln_id, vulndata, vulnextradata):
        # Parse the plugin data. This is where CSV and XML diverge.
        port = vulnextradata['port']
        proto = vulnextradata['proto']
        svcname = vulnextradata['svcname']
        plugin_output = vulnextradata['plugin_output']
        pluginID = vulnextradata['pluginID']

        # check to see if we are to ignore this plugin ID or not.
        if int(pluginID) in nessus_config.get('ignored_plugins'):
            return

        svc_fields = {
            'f_proto': proto,
            'f_number': port,
            'f_name': svcname,
            'f_hosts_id': host_id
        }
        svc_rec = services.get_record(**svc_fields)

        # Nessus only guesses the services (and appends a ? at the end)
        splited = svc_fields['f_name'].split("?")
        if svc_rec is not None:
            if splited[0] != svc_rec.f_name and svc_rec.f_name not in splited[0]:
                svc_fields['f_name'] = "%s | %s" % (svc_rec.f_name, splited[0])
            svc_id = svcs.update_or_insert(_key=svc_rec.id, **svc_fields)
        else:
            svc_fields['f_name'] = splited[0]

        svc_rec = services.get_record(
            create_or_update=True,
            **svc_fields
        )

        # create t_service_vulns entry for this pluginID
        svc_vuln = {
            'f_services_id': svc_rec.id,
            'f_vulndata_id': vuln_id,
            'f_proof': plugin_output
        }

        # you may be a vulnerability if...
        if vulnextradata['exploit_available'] == 'true':
            # if you have exploits available you may be an extra special vulnerability
            svc_vuln['f_status'] = 'vulnerable-exploited'
        elif svcname == 'general':
            # if general service then you may not be a vulnerability
            svc_vuln['f_status'] = 'general'
        elif vulndata['f_severity'] == 0:
            # if there is no severity then you may not be a vulnerability
            svc_vuln['f_status'] = 'general'
        else:
            # you're a vulnerability
            svc_vuln['f_status'] = 'vulnerable'
        db.t_service_vulns.update_or_insert(**svc_vuln)

        ######################################################################################################
        ## Let the parsing of Nessus Plugin Output commence!
        ##
        ## Many Plugins provide useful data in plugin_output. We'll go through the list here and extract
        ## out the good bits and add them to Kvasir's database. Some Plugins will not be added as vulnerabilities
        ## because they're truly informational. This list will change if somebody keeps it up to date.
        ##
        ## TODO: This should be moved into a separate function so we can also process csv data
        ## TODO: Add t_service_info key/value records (standardize on Nexpose-like keys?)
        ##
        ######################################################################################################
        d = {}

        nessus_vulns.stats['added'] += 1
        #### SNMP
        if pluginID == '10264':
            # snmp community strings
            for snmp in re.findall(' - (.*)', plugin_output):
                db.t_snmp.update_or_insert(f_hosts_id=host_id, f_community=snmp)
                db.commit()

        #### SMB/NetBIOS
        if pluginID in ['10860', '10399']:
            # SMB Use Host SID (10860) or Domain SID (10399) to enumerate users
            for user in re.findall(' - (.*)', plugin_output):
                username = user[0:user.find('(')-1]
                try:
                    gid = re.findall('\(id (\d+)', user)[0]
                except:
                    gid = '0'

                # Windows users, local groups, and global groups
                d['f_username'] = username
                d['f_gid'] = gid
                d['f_services_id'] = svc_rec.id
                d['f_source'] = '10860'
                db.t_accounts.update_or_insert(**d)
                db.commit()

        if pluginID == '17651':
            # Microsoft Windows SMB : Obtains the Password Policy
            d['f_hosts_id'] = host_id
            try:
                d['f_lockout_duration'] = re.findall('Locked account time \(s\): (\d+)', plugin_output)[0]
                d['f_lockout_limit'] = re.findall(
                    'Number of invalid logon before locked out \(s\): (\d+)', plugin_output
                )[0]
            except IndexError:
                d['f_lockout_duration'] = 1800
                d['f_lockout_limit'] = 0
            db.t_netbios.update_or_insert(**d)
            db.commit()

        if pluginID == '10395':
            # Microsoft Windows SMB Shares Enumeration
            d['f_hosts_id'] = host_id
            d['f_shares'] = re.findall(' - (.*)', plugin_output)
            db.t_netbios.update_or_insert(**d)

        if pluginID == '10150':
            # Windows NetBIOS / SMB Remote Host Information Disclosure
            try:
                d['f_hosts_id'] = host_id
                d['f_domain'] = re.findall('(\w+).*= Workgroup / Domain name', plugin_output)[0]
                db.t_netbios.update_or_insert(**d)
            except IndexError:
                pass

        #### FTP
        if pluginID == '10092':
            # FTP Server Detection
            RE_10092 = re.compile('The remote FTP banner is :\n\n(.*)', re.DOTALL)
            try:
                d['f_banner'] = RE_10092.findall(plugin_output)[0]
                svc_rec.update(**d)
                db.commit()
                db(db.t_service_info)
                db.t_service_info.update_or_insert(
                    f_services_id=svc_rec.id,
                    f_name='ftp.banner',
                    f_text=d['f_banner']
                )
                db.commit()
            except Exception, e:
                log("Error parsing FTP banner (id 10092): %s" % str(e), logging.ERROR)

        #### SSH
        if pluginID == '10267':
            # SSH Server Type and Version Information
            try:
                ssh_banner, ssh_supported_auth = re.findall('SSH version : (.*)\nSSH supported authentication : (.*)', plugin_output)[0]
                d['f_banner'] = ssh_banner
                svc_rec.update_record(**d)
                db.commit()
                db.t_service_info.update_or_insert(
                    f_services_id=svc_rec.id,
                    f_name='ssh.banner',
                    f_text=d['f_banner']
                )
                db.t_service_info.update_or_insert(
                    f_services_id=svc_rec.id,
                    f_name='ssh.authentication',
                    f_text=ssh_supported_auth
                )
                db.commit()

            except Exception, e:
                log("Error parsing SSH banner (id 10267): %s" % str(e), logging.ERROR)

        if pluginID == '10881':
            # SSH Protocol Versions Supported
            try:
                ssh_versions = re.findall(' - (.*)', plugin_output)
                db.t_service_info.update_or_insert(
                    f_services_id=svc_rec.id,
                    f_name='ssh.versions',
                    f_text=', '.join(ssh_versions)
                )
                db.commit()

            except Exception, e:
                log("Error parsing SSH versions (id 10881): %s" % str(e), logging.ERROR)

            try:
                fingerprint = re.findall('SSHv2 host key fingerprint : (.*)', plugin_output)
                db.t_service_info.update_or_insert(
                    f_services_id=svc_rec.id,
                    f_name='ssh.fingerprint',
                    f_text=fingerprint[0]
                )
                db.commit()

            except Exception, e:
                log("Error parsing SSH fingerprint (id 10881): %s" % str(e), logging.ERROR)

        ### Telnet
        if pluginID in ['10281', '42263']:
            # Telnet banner
            try:
                snip_start = plugin_output.find('snip ------------------------------\n')
                snip_end = plugin_output.rfind('snip ------------------------------\n')
                if snip_start > 0 and snip_end > snip_start:
                    d['f_banner'] = plugin_output[snip_start+36:snip_end-36]
                    svc_rec.update(**d)
                    db.commit()
                else:
                    log("Error finding Telnet banner: (st: %s, end: %s, banner: %s)" %
                        (snip_start, snip_end, plugin_output), logging.ERROR)
            except Exception, e:
                log("Error parsing Telnet banner: %s" % str(e), logging.ERROR)

        ### HTTP Server Info
        if pluginID == '10107':
            # HTTP Server Type and Version
            RE_10107 = re.compile('The remote web server type is :\n\n(.*)', re.DOTALL)
            try:
                d['f_banner'] = RE_10107.findall(plugin_output)[0]
                svc_rec.update(**d)
                db.commit()
                db.t_service_info.update_or_insert(
                    f_services_id=svc_rec.id,
                    f_name='http.banner.server',
                    f_text=d['f_banner']
                )
                db.commit()
            except Exception, e:
                log("Error parsing HTTP banner (id 10107): %s" % str(e), logging.ERROR)

        ### Operating Systems and CPE
        if pluginID == '45590':
            # Common Platform Enumeration (CPE)
            for cpe_os in re.findall('(cpe:/o:.*)', plugin_output):
                os_id = lookup_cpe(cpe_os.replace('cpe:/o:', '').rstrip(' '))
                if os_id:
                    db.t_host_os_refs.update_or_insert(
                        f_certainty='0.90',     # just a stab
                        f_family='Unknown',     # not given in Nessus
                        f_class=hostdata.get('system-type'),
                        f_hosts_id=host_id,
                        f_os_id=os_id
                    )
                    db.commit()

    for host in nessus_iterator:
        if not nessus_csv_type:
            (host_id, hostdata, hostextradata) = nessus_hosts.parse(host.find('HostProperties'))
        else:
            (host_id, hostdata, hostextradata) = nessus_hosts.parse(host)

        if not host_id:
            # no host_id returned, it was either skipped or errored out
            continue

        if not nessus_csv_type:
            # Parse the XML <ReportItem> sections where plugins, ports and output are all in
            for rpt_item in host.iterfind('ReportItem'):
                (vuln_id, vulndata, extradata) = nessus_vulns.parse(rpt_item)
                if not vuln_id:
                    # no vulnerability id
                    continue
                _plugin_parse(host_id, vuln_id, vulndata, extradata)
        else:
            (vuln_id, vulndata, extradata) = nessus_vulns.parse(host)
            _plugin_parse(host_id, vuln_id, vulndata, extradata)

    if msf_settings.get('workspace'):
        try:
            # check to see if we have a Metasploit RPC instance configured and talking
            from MetasploitProAPI import MetasploitProAPI
            msf_api = MetasploitProAPI(host=msf_settings.get('url'), apikey=msf_settings.get('key'))
            working_msf_api = msf_api.login()
        except Exception, error:
            log(" [!] Unable to authenticate to MSF API: %s" % str(error), logging.ERROR)
            working_msf_api = False

        try:
            scan_data = open(filename, "r+").readlines()
        except Exception, error:
            log(" [!] Error loading scan data to send to Metasploit: %s" % str(error), logging.ERROR)
            scan_data = None

        if scan_data and working_msf_api:
            task = msf_api.pro_import_data(
                msf_settings.get('workspace'),
                "".join(scan_data),
                {
                    #'preserve_hosts': form.vars.preserve_hosts,
                    'blacklist_hosts': "\n".join(ip_ignore_list)
                },
                )

            msf_workspace_num = session.msf_workspace_num or 'unknown'
            msfurl = os.path.join(msf_settings.get('url'), 'workspaces', msf_workspace_num, 'tasks', task['task_id'])
            log(" [*] Added file to MSF Pro: %s" % msfurl)

    # any new Nessus vulns need to be checked against exploits table and connected
    log(" [*] Connecting exploits to vulns and performing do_host_status")
    connect_exploits()
    do_host_status(asset_group=asset_group)

    msg = (' [*] Import complete: hosts: %s added, %s updated, %s skipped '
           '- %s vulns processed, %s added' % (
               nessus_hosts.stats['added'],
               nessus_hosts.stats['updated'],
               nessus_hosts.stats['skipped'],
               nessus_vulns.stats['processed'],
               nessus_vulns.stats['added']
           ))
    log(msg)
    return msg
