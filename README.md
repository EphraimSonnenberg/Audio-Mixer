# Audio Mixer

A Wave Link–style software audio mixer for Linux (PipeWire / PulseAudio), built with GTK4 and libadwaita.
No special hardware needed.

- **Channels** (rows): virtual channels that apps are routed into (Games, Voice, Music, …), plus hardware inputs such as microphones.
- **Mixes** (columns): independent buses with their own level for every channel. Each mix can be sent to any number of output devices, and it can also be exposed as a **virtual microphone**, so Discord or OBS can record it.
- **System channel**: catches every sound that isn't assigned to another channel.
- **Apps page**: send any playing app to a channel (remembered), and choose the input that each recording app uses.
- **Virtual Stream Deck**: pages of keys for mute, volume, routing, system stats (CPU/GPU/RAM/network/disk), media control, launching apps, websites and commands. Icons, images and colours are customizable. It can pop out into a resizable, lockable, always-on-top window.
- **System tray**: closing the window keeps the mixer running in the tray.
- **OpenDeck plugin** (`opendeck/`): control the mixer from a real Stream Deck through [OpenDeck](https://github.com/nekename/OpenDeck).

Audio passes through untouched. At 100% faders the signal is bit-identical; the graph stays at the server's sample rate in 32-bit float. The only cost is about 20 ms of loopback latency per hop.

## Requirements

- PipeWire with `pipewire-pulse` (or PulseAudio), plus `pactl` and `parec`
- Python 3.10+, PyGObject, GTK 4, libadwaita 1.5+, pycairo
- Optional: KDE Plasma (KWin), for the deck's always-on-top and hide-from-taskbar options; `nvidia-smi` for NVIDIA GPU stats

Arch / CachyOS: `sudo pacman -S python-gobject gtk4 libadwaita python-cairo libpulse`

## Run

```sh
python3 audio_mixer.py
```

Optional desktop launcher (replace the path):

```ini
# ~/.local/share/applications/io.github.lawsonf.AudioMixer.desktop
[Desktop Entry]
Type=Application
Name=Audio Mixer
Exec=python3 /path/to/audio_mixer.py
Icon=io.github.lawsonf.AudioMixer
Categories=AudioVideo;Audio;Mixer;
```

Then copy `assets/io.github.lawsonf.AudioMixer.svg` to `~/.local/share/icons/hicolor/scalable/apps/`.

Settings are saved in `~/.config/audio-mixer/config.json`. Quitting from the tray or sidebar removes all virtual devices and restores your default output and microphone.

## OpenDeck plugin

```sh
sh opendeck/install.sh
```

Restart OpenDeck afterwards. The plugin talks to the mixer through a local socket at `$XDG_RUNTIME_DIR/audio-mixer.sock`, which takes JSON lines. The protocol is documented in `ControlServer` in `audio_mixer.py`.
