"Function to parse responses coming in, used by multiple classes"

import logging
from urllib.parse import unquote

_LOGGER = logging.getLogger(__name__)


def parse_response(response_body):
    """Parse response from Daikin.

    Returns the parsed key/value pairs on success. When the device rejects
    the request (ret != OK) the rejection marker is preserved as
    {'ret': <value>} so callers can distinguish an explicit rejection from
    a legitimately empty OK response; 'ret' is popped on success, so
    `'ret' in parsed` <=> the device rejected the request.
    """
    _LOGGER.debug("Parsing response: %s", response_body)
    # Deterministic split-and-rejoin parser: values containing '=' are
    # preserved (the old regex dropped or mis-keyed them) and a comma
    # inside a value is glued back onto the previous pair.
    pairs = []
    for segment in response_body.split(','):
        if '=' in segment:
            key, value = segment.split('=', 1)
            pairs.append((key, value))
        elif pairs:
            pairs[-1] = (pairs[-1][0], f"{pairs[-1][1]},{segment}")
        else:
            _LOGGER.debug("Ignoring malformed leading segment: %r", segment)
    response = dict(pairs)
    if 'ret' not in response:
        raise ValueError("missing 'ret' field in response")
    ret = response.pop('ret')
    if ret != 'OK':
        _LOGGER.debug("Non-OK Daikin response: ret=%s", ret)
        return {'ret': ret}
    if 'name' in response:
        response['name'] = unquote(response['name'])
    return response
