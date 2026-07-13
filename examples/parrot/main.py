# A guest you can talk to. This is what the Console tab is for — not a log with
# extra steps, but a serial line into a running program, the way a VM's console
# is a serial line into a running kernel.
#
# Type into it: `help`, `heap`, `echo something`, `pub topic {"a":1}`, `quit`.

HELP = 'commands: help · heap · echo <text> · pub <topic> <json> · quit'


async def run(hal):
    hal.console.write('parrot is listening.', HELP)

    async for line in hal.console.input():
        line = line.strip()
        if not line:
            continue
        verb, _, rest = line.partition(' ')

        if verb == 'help':
            hal.console.write(HELP)
        elif verb == 'heap':
            heap = hal.bus.retained('$sys/heap')
            hal.console.write('heap free:', heap['free'] if heap else 'unknown')
        elif verb == 'echo':
            hal.console.write(rest)
        elif verb == 'pub':
            topic, _, payload = rest.partition(' ')
            try:
                import json
                hal.bus.publish(topic, json.loads(payload or 'null'))
                hal.console.write('published', topic)
            except Exception as e:
                hal.console.write('nope:', e)
        elif verb == 'quit':
            hal.console.write('bye.')
            return                      # a clean exit; the supervisor stops us
        else:
            hal.console.write('unknown:', verb, '·', HELP)
