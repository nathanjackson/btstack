#!/usr/bin/env python3
# BlueKitchen GmbH (c) 2019

# primitive dump for PacketLogger format

# APPLE PacketLogger
# typedef struct {
#   uint32_t    len;
#   uint32_t    ts_sec;
#   uint32_t    ts_usec;
#   uint8_t     type;   // 0xfc for note
# }

#define BLUETOOTH_DATA_TYPE_PB_ADV                                             0x29 // PB-ADV
#define BLUETOOTH_DATA_TYPE_MESH_MESSAGE                                       0x2A // Mesh Message
#define BLUETOOTH_DATA_TYPE_MESH_BEACON                                        0x2B // Mesh Beacon

import re
import sys
import time
import datetime
import struct
from mesh_crypto import *

# state
netkeys = {}
appkeys = {}
devkey = b''
ivi = b'\x00'
segmented_messages = {}


# helpers
def read_net_32_from_file(f):
    data = f.read(4)
    if len(data) < 4:
        return -1
    return struct.unpack('>I', data)[0]

def as_hex(data):
    return ''.join(["{0:02x} ".format(byte) for byte in data])

def as_big_endian32(value):
    return struct.pack('>I', value)

def read_net_16(data):
    return struct.unpack('>H', data)[0]

def read_net_24(data):
    return data[0] << 16 | struct.unpack('>H', data[1:3])[0]

# log engine - simple pretty printer
max_indent = 0
def log_pdu(pdu, indent = 0, hide_properties = []):
    spaces = '    ' * indent
    print(spaces + pdu.type)
    if len(pdu.status) > 0:
        print (spaces + '|           status: ' + pdu.status)
    for property in pdu.properties:
        if property.key in hide_properties:
            continue
        if isinstance( property.value, int):
            print (spaces + "|%15s: 0x%x (%u)" % (property.key, property.value, property.value))
        elif isinstance( property.value, bytes):
            print (spaces + "|%15s: %s" % (property.key, as_hex(property.value)))
        else:
            print (spaces + "|%15s: %s" % (property.key, str(property.value)))
        hide_properties.append(property.key)
    print (spaces + '|           data: ' + as_hex(pdu.data))
    if indent >= max_indent:
        return
    print (spaces + '----')
    for origin in pdu.origins:
        log_pdu(origin, indent + 1, hide_properties)

# classes

class network_key(object):
    def __init__(self, index, netkey):
        self.index = index
        self.netkey = netkey
        (self.nid, self.encryption_key, self.privacy_key) = k2(netkey, b'\x00')

    def __repr__(self):
        return ("NetKey-%04x %s: NID %02x Encryption %s Privacy %s" % (self.index, self.netkey.hex(), self.nid, self.encryption_key.hex(), self.privacy_key.hex()))

class application_key(object):
    def __init__(self, index, appkey):
        self.index = index
        self.appkey = appkey
        self.aid = k4(self.appkey)

    def __repr__(self):
        return ("AppKey-%04x %s: AID %02x" % (self.index, self.appkey.hex(), self.aid))

class property(object):
    def __init__(self, key, value):
        self.key = key
        self.value = value

class layer_pdu(object):
    def __init__(self, pdu_type, pdu_data):
        self.status = ''
        self.src = None
        self.dst = None
        self.type = pdu_type
        self.data = pdu_data
        self.origins = []
        self.properties = []

    def add_property(self, key, value):
        self.properties.append(property(key, value))

class network_pdu(layer_pdu):
    def __init__(self, pdu_data):
        super().__init__("Network(unencrpyted)", pdu_data)

        # parse pdu
        self.ivi = (self.data[1] & 0x80) >> 7
        self.nid = self.data[0] & 0x7f
        self.ctl = (self.data[1] & 0x80) == 0x80
        self.ttl = self.data[1] & 0x7f
        self.seq = read_net_24(self.data[2:5])
        self.src = read_net_16(self.data[5:7])
        self.dst = read_net_16(self.data[7:9])
        self.lower_transport = self.data[9:]

        # set properties
        self.add_property('ivi', self.ivi)
        self.add_property('nid', self.nid)
        self.add_property('ctl', self.ctl)
        self.add_property('ttl', self.ttl)
        self.add_property('seq', self.seq)
        self.add_property('src', self.src)
        self.add_property('dst', self.dst)
        self.add_property('lower_transport', self.lower_transport)

class lower_transport_pdu(layer_pdu):
    def __init__(self, network_pdu):
        super().__init__('Lower Transport', network_pdu.lower_transport)

        # inherit properties
        self.ctl = network_pdu.ctl
        self.seq = network_pdu.seq
        self.src = network_pdu.src
        self.dst = network_pdu.dst
        self.add_property('ctl', self.ctl)
        self.add_property('seq', self.seq)
        self.add_property('src', self.src)
        self.add_property('dst', self.dst)

        # parse pdu and set propoerties
        self.seg = (self.data[0] & 0x80) == 0x80
        self.add_property('seg', self.seg)
        self.szmic = False
        if self.ctl:
            self.opcode = self.data[0] & 0x7f
            self.add_property('opcode', self.opcode)
        else:
            self.aid = self.data[0] & 0x3f 
            self.add_property('aid', self.aid)
            self.akf = self.data[0] & 0x40 == 0x040
            self.add_property('akf', self.akf)
        if self.seg:
            if self.ctl:
                self.szmic = self.data[1] & 0x80 == 0x80
                self.add_property('szmic', self.szmic)
            temp_12 = struct.unpack('>H', self.data[1:3])[0]
            self.seq_zero = (temp_12 >> 2) & 0x1fff
            self.add_property('seq_zero', self.seq_zero)
            temp_23 = struct.unpack('>H', self.data[2:4])[0]
            self.seg_o = (temp_23 >> 5) & 0x1f
            self.add_property('seg_o', self.seg_o)
            self.seg_n =  temp_23 & 0x1f
            self.add_property('seg_n', self.seg_n)
            self.segment = self.data[4:]
            self.add_property('segment', self.segment)
        else:
            self.upper_transport = self.data[1:]
            self.add_property('upper_transport', self.upper_transport)

class uppert_transport_pdu(layer_pdu):
    def __init__(self, segment):
        if segment.ctl:
            super().__init__('Segmented Control', b'')
        else:
            super().__init__('Segmented Transport', b'')
        self.ctl      = segment.ctl
        self.src      = segment.src
        self.dst      = segment.dst
        self.seq      = segment.seq
        self.akf      = segment.akf
        self.aid      = segment.aid
        self.szmic    = segment.szmic
        self.seg_n    = segment.seg_n
        self.seq_zero = segment.seq_zero
        # TODO handle seq_zero overrun
        self.seq_auth = segment.seq & 0xffffe000 | segment.seq_zero
        self.add_property('seq_auth', self.seq_auth)
        self.missing  = (1 << (segment.seg_n+1)) - 1
        self.data = b''
        self.processed = False
        self.origins = []
        if self.ctl:
            self.segment_len = 8
        else:
            self.segment_len = 12

        self.add_property('src', self.src)
        self.add_property('dst', self.dst)
        self.add_property('segment_len', self.segment_len)

    def add_segment(self, network_pdu):
        self.origins.append(network_pdu)
        self.missing &= ~ (1 << network_pdu.seg_o)
        if network_pdu.seg_o == self.seg_n:
            # last segment, set len
            self.len = (self.seg_n * self.segment_len) + len(network_pdu.segment)
        if len(self.data) == 0 and self.complete():
            self.reassemble()

    def complete(self):
        return self.missing == 0

    def reassemble(self):
        self.data = bytearray(self.len)
        missing  = (1 << (self.seg_n+1)) - 1
        for pdu in self.origins:
            # copy data
            pos = pdu.seg_o * self.segment_len
            self.data[pos:pos+len(pdu.segment)] = pdu.segment
            # done?
            missing &= ~ (1 << pdu.seg_o)
            if missing == 0:
                break

class access_pdu(layer_pdu):
    def __init__(self, lower_pdu, data):
        super().__init__('Access', b'')
        self.src      = lower_pdu.src
        self.dst      = lower_pdu.dst
        self.akf      = lower_pdu.akf
        self.aid      = lower_pdu.aid
        self.data     = data
        self.add_property('src', self.src)
        self.add_property('dst', self.dst)
        self.add_property('akf', self.akf)
        self.add_property('aid', self.aid)

def segmented_message_for_pdu(pdu):
    if pdu.src in segmented_messages:
        seg_message = segmented_messages[pdu.src]
        # check seq zero
    else:
        seg_message = uppert_transport_pdu(pdu)
        segmented_messages[pdu.src] = seg_message
    return seg_message

def mesh_set_iv_index(iv_index):
    global ivi
    ivi = iv_index
    print ("IV-Index: " + as_big_endian32(ivi).hex())

# key management
def mesh_add_netkey(index, netkey):
    key = network_key(index, netkey)
    print (key)
    netkeys[index] = key

def mesh_network_keys_for_nid(nid):
    for (index, key) in netkeys.items():
        if key.nid == nid:
            yield key

def mesh_set_device_key(key):
    global devkey
    print ("DevKey: " + key.hex())
    devkey = key

def mesh_add_application_key(index, appkey):
    key = application_key(index, appkey)
    print (key)
    appkeys[index] = key

def mesh_application_keys_for_aid(aid):
    for (index, key) in appkeys.items():
        if key.aid == aid:
            yield key

def mesh_transport_nonce(pdu, nonce_type):
    if pdu.szmic:
        aszmic = 0x80
    else:
        aszmic = 0x00
    return bytes( [nonce_type, aszmic, pdu.seq_auth >> 16, (pdu.seq_auth >> 8) & 0xff, pdu.seq_auth & 0xff, pdu.src >> 8, pdu.src & 0xff, pdu.dst >> 8, pdu.dst & 0xff]) + as_big_endian32(ivi)

def mesh_application_nonce(pdu):
    return mesh_transport_nonce(pdu, 0x01)

def mesh_device_nonce(pdu):
    return mesh_transport_nonce(pdu, 0x02)

def mesh_upper_transport_decrypt(message, data):
    if message.szmic:
        trans_mic_len = 8
    else:
        trans_mic_len = 4
    ciphertext = data[:-trans_mic_len]
    trans_mic  = data[-trans_mic_len:]
    nonce      = mesh_device_nonce(message)
    decrypted = None
    if message.akf:
        for key in mesh_application_keys_for_aid(message.aid):
            decrypted = aes_ccm_decrypt(key.appkey, nonce, ciphertext, b'', trans_mic_len, trans_mic)
            if decrypted != None:
                break
    else:
        decrypted =  aes_ccm_decrypt(devkey, nonce, ciphertext, b'', trans_mic_len, trans_mic)
    return decrypted

def mesh_process_control(control_pdu):
    # TODO decode control message
    # TODO add Seg Ack to sender access message origins
    log_pdu(control_pdu, 0, [])

def mesh_proess_access(access_pdu):
    log_pdu(access_pdu, 0, [])

def mesh_process_network_pdu_tx(network_pdu_encrypted):

    # network layer - decrypt pdu
    nid = network_pdu_encrypted.data[0] & 0x7f
    for key in mesh_network_keys_for_nid(nid):
        network_pdu_decrypted_data = network_decrypt(network_pdu_encrypted.data, as_big_endian32(ivi), key.encryption_key, key.privacy_key)
        if network_pdu_decrypted_data != None:
            break
    if network_pdu_decrypted_data == None:
        network_pdu_encrypted.status = 'No encryption key found'
        log_pdu(network_pdu_encrypted, 0, [])
        return

    # decrypted network pdu
    network_pdu_decrypted = network_pdu(network_pdu_decrypted_data)
    network_pdu_decrypted.origins.append(network_pdu_encrypted)

    # lower transport - reassemble
    lower_transport = lower_transport_pdu(network_pdu_decrypted)
    lower_transport.origins.append(network_pdu_decrypted)

    if lower_transport.seg:
        message = segmented_message_for_pdu(lower_transport)
        message.add_segment(lower_transport)
        if not message.complete():
            return
        if message.processed:
            return

        message.processed = True
        if message.ctl:
            mesh_process_control(message)
        else:
            access_payload = mesh_upper_transport_decrypt(message, message.data)
            if access_payload == None:
                message.status = 'No encryption key found'
                log_pdu(message, 0, [])
            else:
                access = access_pdu(message, access_payload)
                access.origins.append(message)
                mesh_proess_access(access)

    else:
        if lower_transport.ctl:
            control = layer_pdu('Unsegmented Control', lower_transport.data)
            control.origins.add(lower_transport)
            mesh_process_control(control)
        else:
            access_payload = mesh_upper_transport_decrypt(lower_transport, lower_transport.upper_transport)
            if access_payload == None:
                lower_transport.status = 'No encryption key found'
                log_pdu(lower_transport, 0, [])
            else:
                access = access_pdu(lower_transport, access_payload)
                access.add_property('seq_auth', lower_transport.seq)
                access.origins.append(lower_transport)
                mesh_proess_access(access)


def mesh_process_beacon_pdu(adv_pdu):
    log_pdu(adv_pdu, 0, [])

def mesh_process_adv(adv_pdu):
    ad_type = adv_pdu.data[1]
    if ad_type == 0x2A:
        network_pdu_encrypted = layer_pdu("Network(encrypted)", adv_data[2:])
        network_pdu_encrypted.add_property('ivi', adv_data[2] >> 7)
        network_pdu_encrypted.add_property('nid', adv_data[2] & 0x7f)
        network_pdu_encrypted.origins.append(adv_pdu)
        mesh_process_network_pdu_tx(network_pdu_encrypted)
    if ad_type == 0x2b:
        beacon_pdu = layer_pdu("Beacon", adv_data[2:])
        beacon_pdu.origins.append(adv_pdu)
        mesh_process_beacon_pdu(beacon_pdu)


if len(sys.argv) == 1:
    print ('Dump Mesh PacketLogger file')
    print ('Copyright 2019, BlueKitchen GmbH')
    print ('')
    print ('Usage: ' + sys.argv[0] + 'hci_dump.pklg')
    exit(0)

infile = sys.argv[1]

with open (infile, 'rb') as fin:
    pos = 0
    while True:
        payload_length  = read_net_32_from_file(fin)
        if payload_length < 0:
            break
        ts_sec  = read_net_32_from_file(fin)
        ts_usec = read_net_32_from_file(fin)
        type    = ord(fin.read(1))
        packet_len = payload_length - 9;
        if (packet_len > 66000):
            print ("Error parsing pklg at offset %u (%x)." % (pos, pos))
            break

        packet  = fin.read(packet_len)
        pos     = pos + 4 + payload_length
        # time    = "[%s.%03u] " % (datetime.datetime.fromtimestamp(ts_sec).strftime("%Y-%m-%d %H:%M:%S"), ts_usec / 1000)

        if type == 0:
            # CMD
            if packet[0] != 0x08:
                continue
            if packet[1] != 0x20:
                continue
            adv_data = packet[4:]
            adv_pdu = layer_pdu("ADV", adv_data)
            mesh_process_adv(adv_pdu)

        elif type == 1:
            # EVT
            event = packet[0]
            if event != 0x3e:
                continue
            event_len = packet[1]
            if event_len != 0x2b:
                continue
            adv_data = packet[13:-1]
            adv_pdu = layer_pdu("ADV", adv_data)
            mesh_process_adv(adv_pdu)

        elif type == 0xfc:
            # LOG
            log = packet.decode("utf-8")
            parts = re.match('mesh-iv-index: (.*)', log)
            if parts and len(parts.groups()) == 1:
                mesh_set_iv_index(int(parts.groups()[0], 16))
                continue
            parts = re.match('mesh-devkey: (.*)', log)
            if parts and len(parts.groups()) == 1:
                mesh_set_device_key(bytes.fromhex(parts.groups()[0]))
                continue
            parts = re.match('mesh-appkey-(.*): (.*)', log)
            if parts and len(parts.groups()) == 2:
                mesh_add_application_key(int(parts.groups()[0], 16), bytes.fromhex(parts.groups()[1]))
                continue
            parts = re.match('mesh-netkey-(.*): (.*)', log)
            if parts and len(parts.groups()) == 2:
                mesh_add_netkey(int(parts.groups()[0], 16), bytes.fromhex(parts.groups()[1]))
                continue
