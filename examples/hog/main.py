# The ungovernable guest (MILESTONES M5). It never yields, so it starves the
# event loop — the heartbeat, the flagging logic, and the web server with it.
#
# This is the guest the whole §1 watchdog design exists for: the hardware WDT
# resets the node, the current-guest register in RTC memory survives the reset
# and names the culprit, its autostart is disabled and it is badged. The node
# comes back reachable with the guilty party benched.
#
# Do not start this on a node you need. That is the point of it.
async def run(hal):
    hal.log('about to stop yielding. see you after the watchdog.')
    while True:
        pass
