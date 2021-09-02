#!env/bin/python

from cachetools.func import ttl_cache
from hashable_cache import hashable_cache
from hbmqtt.mqtt.constants import QOS_0
from hbmqtt.client import MQTTClient, ConnectException, ClientException
import json
import asyncio
import requests
import logging
import yaml
import io
import argparse
import re

def get_from_dict(config: dict, name: str, default = None):
    names = name.split('.')
    result = None
    if len(names) > 0:
        while config is not None:
            n = names.pop(0)
            result = config.get(n, None)
            config = result if len(names) > 0 else None
    return result if result is not None else default

@hashable_cache(ttl_cache(maxsize=128, ttl=300))
def get_cached_devices(config: dict):
    log = logging.getLogger('mqtt2deconz.get_cached_devices')
    log.info('Requesting devices from deCONZ')
    
    endpoint = str(get_from_dict(config, 'deconz.uri')) + '/api/' + str(get_from_dict(config, 'deconz.apikey')) + '/lights'
    r = requests.get(url=endpoint)
    
    if r is None or r == '':
        log.warn('I got a null or empty string value for data from deconz')
        return []
    
    data = r.json()
    if not isinstance(data, dict):
        log.warn('Error. Check if deconz.apikey was provided in the configuration yaml.')
        return []
        
    return data.keys()

async def mqtt_subscriber(config: dict, message_queue: asyncio.Queue) -> None:
    log = logging.getLogger('mqtt2deconz.mqtt_subscriber')
    mqtt = MQTTClient(config=get_from_dict(config, 'mqtt.client'))
    log.info('Connecting to MQTT...')

    # Connecting to MQTT
    try:
        await mqtt.connect(uri=get_from_dict(config, 'mqtt.client.uri'), cleansession=get_from_dict(config, 'mqtt.client.cleansession'))
    except ConnectException as ce:
        log.error('Can\'t connect to MQTT: {}'.format(ce))
        return
    log.info('Connected to MQTT')
    
    # Getting device topic tuples list
    prefix = str(get_from_dict(config, 'mqtt.topic_prefix'))
    device_topics = [prefix + '/lights/' + key + '/cmnd' for key in get_cached_devices(config)]
    
    # Subscribing to topics for every device received from discovery
    await mqtt.subscribe(list(zip(device_topics, [QOS_0] * len(device_topics))))
    
    # Pattern to isolate device number
    p = re.compile('deconz\/lights\/(\d+)\/cmnd')

    # Waiting for messages
    while True:
        try:
            try:
                message = await mqtt.deliver_message()
                packet = message.publish_packet
                device_id = p.match(packet.variable_header.topic_name).group(1)
                message_data = packet.payload.data
                
                log.debug('Got message: {}'.format(message_data))
                log.debug('Got device id: {}'.format(device_id))
                
                message_json = json.loads(message_data)
                message_json['type'] = 'lights'
                message_json['id'] = device_id

                message_queue.put_nowait(json.dumps(message_json, indent = 0))
            except asyncio.TimeoutError as te:
                log.info('Timeout. Refreshing subscription')
                await mqtt.unsubscribe(device_topics)
                device_topics = [prefix + '/lights/' + key + '/cmnd' for key in get_cached_devices(config)]
                await mqtt.subscribe(list(zip(device_topics, [QOS_0] * len(device_topics))))
        except (ClientException, AttributeError) as error:
            log.error('Client exception to MQTT occurred')
            asyncio.sleep(60)
    await mqtt.unsubscribe(device_topics)
    await mqtt.disconnect()

async def deconz_message_writer(config: dict, message_queue: asyncio.Queue) -> None:
    log = logging.getLogger('deconz2mqtt.deconz_message_writer')
    headers = {'Content-Type': 'application/json'}
    while True:
        message = await message_queue.get()
        message_json = json.loads(message)
        filtered_message_json = {k: v for k, v in message_json.items() if k in ['on', 'bri']}
        
        id = message_json.get('id', None)
        if 'toggle' in message_json.keys():
            # get current on status
            endpoint = str(get_from_dict(config, 'deconz.uri')) + '/api/' + str(get_from_dict(config, 'deconz.apikey')) + '/lights/' + str(id)
            filtered_message_json['on'] = not get_from_dict(requests.get(url=endpoint).json(), 'state.on')
        
        endpoint = str(get_from_dict(config, 'deconz.uri')) + '/api/' + str(get_from_dict(config, 'deconz.apikey')) + '/lights/' + str(id) + '/state'
        requests.put(endpoint, data=json.dumps(filtered_message_json, indent = 0), headers=headers)

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
