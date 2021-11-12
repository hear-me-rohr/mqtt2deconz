# mqtt2deconz
Simple bridge between MQTT broker and [Conbee II](https://phoscon.de/en/conbee2) (its deCONZ REST API, specifically for the [light endpoint](https://dresden-elektronik.github.io/deconz-rest-doc/endpoints/lights/) and the [group endpoint](https://dresden-elektronik.github.io/deconz-rest-doc/endpoints/groups/)).

This project was heavily influenced by [Michał Kozak](https://github.com/mikozak)'s reverse bridge [deconz2mqtt](https://github.com/mikozak/deconz2mqtt) which picks up broadcasts emitted by deCONZ's websocket API and redirects them to appropriate MQTT topics. 

*mqtt2deconz* is lightweight enough to be run as a service on a Raspberry Pi Zero (completing a deCONZ installation running on the same device). 
It connects to a configured MQTT broker and subscribes to topics mapped to the devices (currently only lights and groups are supported) that are managed by Conbee II\*. Then, it continuously reads incoming messages, parses them and translates them to commands that it immediately sends via the deCONZ REST API.

\*In order to obtain a list of managed devices, *mqtt2deconz* sends a discovery call to deCONZ the results of which are cached for a certain amount of time to prevent too frequent requests.

Let's have a look at the following example:
deCONZ's light discovery API indicates that Conbee II manages a light with the id "57". *mqtt2deconz* will automatically listen to the topic `deconz/lights/57/cmnd` (`deconz` part of the topic can be configured). There, the following message is published:
```
{"on":true, "bri":180}
```
This messages is translated into the following PUT request to the deCONZ API:
```
PUT http://<deCONZ-REST-URI>/api/<apikey>/lights/57
Payload: { "on": true, "bri": 180 }
```
