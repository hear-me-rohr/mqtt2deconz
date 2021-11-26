#!env/bin/python

from cachetools.func import ttl_cache
from hashable_cache import hashable_cache
from hbmqtt.mqtt.constants import QOS_0
from hbmqtt.client import MQTTClient, ConnectException, ClientException
from asyncio import IncompleteReadError
import json
import asyncio
import requests
import logging
import yaml
import io
import argparse
import re


device_types = ['lights', 'groups']
device_type_and_id_regex = 'deconz\/(lights|groups)\/(\d+)\/cmnd'
deconz_uri = 'deconz.uri'
deconz_apikey = 'deconz.apikey'
headers = {'Content-Type': 'application/json'}


def get_from_dict(config: dict, name: str, default = None):
    names = name.split('.')
    result = None
    if len(names) > 0:
        while config is not None:
            n = names.pop(0)
            result = config.get(n, None)
            config = result if len(names) > 0 else None
    return result if result is not None else default


@hashable_cache(ttl_cache(maxsize=128, ttl=600))
def get_cached_devices(config: dict):
    log = logging.getLogger('mqtt2deconz.get_cached_devices')
    log.debug('Requesting devices from deCONZ')
    
    device_type_to_device_keys = {}
    for device_type in device_types:
        endpoint = str(get_from_dict(config, 'deconz.uri')) + '/api/' + str(get_from_dict(config, 'deconz.apikey')) + '/' + device_type
        r = requests.get(url=endpoint)

        if r is None or r == '':
            log.warn('I got a null or empty string value for data from deconz')
            continue

        data = r.json()
        if not isinstance(data, dict):
            log.warn('Error. Check if deconz.apikey was provided in the configuration yaml.')
            continue
            
        device_type_to_device_keys[device_type] = data.keys()
    log.debug('Retrieved devices: ' + str(device_type_to_device_keys))
    return device_type_to_device_keys


def extract_device_topics(config: dict, prefix: str):
    log = logging.getLogger('mqtt2deconz.extract_device_topics')
    devices = get_cached_devices(config)
    device_topics = []
    for device_type in devices.keys():
        device_topics.extend([['/'.join([prefix, device_type, device_id, 'cmnd']) for device_id in devices[device_type]]])
    flattened_device_topics = [item for sublist in device_topics for item in sublist]
    log.debug('Retrieved device topics: ' + str(flattened_device_topics))
    return flattened_device_topics


async def mqtt_subscriber(config: dict, message_queue: asyncio.Queue) -> None:
    log = logging.getLogger('mqtt2deconz.mqtt_subscriber')
    mqtt = MQTTClient(config=get_from_dict(config, 'mqtt.client'))
    log.info('Connecting to MQTT...')

    # Connecting to MQTT
    try:
        await mqtt.connect(uri=get_from_dict(config, 'mqtt.client.uri'), cleansession=get_from_dict(config, 'mqtt.client.cleansession'))
    except (IncompleteReadError, ConnectException) as ce:
        log.error('Can\'t connect to MQTT: {}'.format(ce))
        raise SystemExit('Let\'s hope systectl will restart me...')
    log.info('Connected to MQTT')

    # Getting device topic tuples list
    prefix = str(get_from_dict(config, 'mqtt.topic_prefix'))
    device_topics = extract_device_topics(config, prefix)

    # Subscribing to topics for every device received from discovery
    try:
        await mqtt.subscribe(list(zip(device_topics, [QOS_0] * len(device_topics))))
        log.info('Subscribed to topics: ' + str(device_topics))

        # Pattern to isolate device number
        p = re.compile(device_type_and_id_regex)

        # Waiting for messages
        while True:
            try:
                message = await mqtt.deliver_message(timeout=180)
                packet = message.publish_packet
                device_type = p.match(packet.variable_header.topic_name).group(1)
                device_id = p.match(packet.variable_header.topic_name).group(2)
                message_data = packet.payload.data

                log.info('Got message: {}'.format(message_data))
                log.info('Got device type: {}'.format(device_type))
                log.info('Got device id: {}'.format(device_id))

                message_json = json.loads(message_data)
                message_json['type'] = device_type
                message_json['id'] = device_id

                message_queue.put_nowait(json.dumps(message_json, indent = 0))
            except asyncio.TimeoutError as te:
                log.debug('Timeout. Refreshing subscription')
                await mqtt.unsubscribe(device_topics)
                device_topics = extract_device_topics(config, prefix)
                await mqtt.subscribe(list(zip(device_topics, [QOS_0] * len(device_topics))))
        await mqtt.unsubscribe(device_topics)
        await mqtt.disconnect()

    except (ClientException, AttributeError, IncompleteReadError) as anerror:
        log.error('Client exception to MQTT occurred')
        raise SystemExit('Let\'s hope systectl will restart me...')


def deconz_change_groups(config: dict, do_toggle: bool, an_id: int, message_json: dict):
    endpoint = '/'.join([str(get_from_dict(config, deconz_uri)), 'api', str(get_from_dict(config, deconz_apikey)), 'groups', str(an_id), 'action'])
    if do_toggle:
        filtered_message_json = {k: v for k, v in message_json.items() if k in ['toggle']}
    else:    
        filtered_message_json = {k: v for k, v in message_json.items() if k in ['on', 'bri']}
    requests.put(endpoint, data=json.dumps(filtered_message_json, indent = 0), headers=headers)


def deconz_change_lights(config: dict, do_toggle: bool, an_id: int, message_json: dict):
    filtered_message_json = {k: v for k, v in message_json.items() if k in ['on', 'bri']}
    if do_toggle:
        # GET current on status
        get_endpoint = '/'.join([str(get_from_dict(config, deconz_uri)), 'api', str(get_from_dict(config, deconz_apikey)), 'lights', str(an_id)])
        filtered_message_json['on'] = not get_from_dict(requests.get(url=get_endpoint).json(), 'state.on')
    put_endpoint = '/'.join([str(get_from_dict(config, deconz_uri)), 'api', str(get_from_dict(config, deconz_apikey)), 'lights', str(an_id), 'state'])
    requests.put(put_endpoint, data=json.dumps(filtered_message_json, indent = 0), headers=headers)


async def deconz_message_writer(config: dict, message_queue: asyncio.Queue) -> None:
    log = logging.getLogger('deconz2mqtt.deconz_message_writer')
    while True:
        message = await message_queue.get()
        message_json = json.loads(message)        
        do_toggle = 'toggle' in message_json.keys()
        an_id = message_json.get('id', None)        
        
        if (message_json.get('type', None) == 'lights'):
            deconz_change_lights(config, do_toggle, an_id, message_json)
        else:
            deconz_change_groups(config, do_toggle, an_id, message_json)
        

async def main(config: dict):
    message_queue = asyncio.Queue(10)
    mqtt = asyncio.create_task(mqtt_subscriber(config, message_queue))
    deconz = asyncio.create_task(deconz_message_writer(config, message_queue))
    done, pending = await asyncio.wait([mqtt, deconz], return_when=asyncio.FIRST_EXCEPTION)
    
    for task in done:
        task.result()
    for task in pending:
        task.cancel()

if __name__ == "__main__":
    # parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()

    # read config file
    with io.open(args.config, 'r') as stream:
        config = yaml.safe_load(stream)

    # configure logging
    logging.basicConfig(
        format='%(asctime)s %(levelname)s:%(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')
    for logger_name, logger_level in get_from_dict(config, 'logging', {}).items():
        logging.getLogger(None if logger_name == 'root' else logger_name).setLevel(logger_level)
        
    asyncio.run(main(config))
