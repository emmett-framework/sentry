# Emmett-Sentry

Emmett-Sentry is an [Emmett framework](https://emmett.sh) extension integrating [Sentry](https://sentry.io) monitoring platform.

[![pip version](https://img.shields.io/pypi/v/emmett-sentry.svg?style=flat)](https://pypi.python.org/pypi/emmett-sentry) 

## Installation

You can install Emmett-Sentry using pip:

    pip install emmett-sentry

And add it to your Emmett application:

```python
from emmett_sentry import Sentry

sentry = app.use_extension(Sentry)
```

## Configuration

Here is the complete list of parameters of the extension configuration:

| param | default | description |
| --- | --- | --- |
| dsn | | Sentry project's DSN |
| environment | development | Application environment |
| release | | Application release |
| auto\_load | `True` | Automatically inject extension on routes |
| sample\_rate | 1 | Error sampling rate |
| integrations | | List of integrations to pass to the SDK |
| enable\_tracing | `False` | Enable tracing on routes |
| tracing\_sample\_rate | | Traces sampling rate |
| tracing\_exclude\_routes | | List of specific routes to exclude from tracing | 
| trace\_websockets | `False` | Enable tracing on websocket routes |
| trace\_orm | `True` | Enable tracing on ORM queries |
| trace\_templates | `True` | Enable tracing on templates rendering |
| trace\_sessions | `True` | Enable tracing on sessions load/store |
| trace\_cache | `True` | Enable tracing on cache get/set |
| trace\_pipes | `False` | Enable tracing on pipes |

## Usage

The extension exposes two methods to manually track events:

- exception
- message

You call these methods directly within your code:

```python
# track an error
try:
    1 / 0
except Exception:
    sentry.exception()

# track a message
sentry.message("some event", level="info")
```

## License

Emmett-Sentry is released under BSD license.
