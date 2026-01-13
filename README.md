# Sonos-Streamer
This app takes audio input from computer default speaker and creates a streaming audio file that can easily be accessed by most smart speakers.

## The problem
Airparrot wouldn't work on my computer, and honestly would sometimes take too much effort to stream. I wanted to be able to just use the Sonos app as the main controller for what is coming through the Sonos system.

## The aim
The aim was ideally a one file build by compiling in ffmpeg etc. and using native audio libraries.
Allow for control mainly from Sonos app.

## The result
An easy way to resolve streaming any device audio to speakers though the latency is large and perhaps i'll look into reducing this later.
With this current setup I receive my ideal. Control via the Sonos app and an exe that requires 0 maintaining on my laptop that can just startup with my computer. I just create a TuneIn custom radio station set to my local ip like so:
http://192.168.1.xxx:9000/stream

Download and use it easily as an exe or app.

![GitHub Release](https://img.shields.io/github/v/release/fauly/sonos-streamer)

Please share any further ideas for development or improvement.
