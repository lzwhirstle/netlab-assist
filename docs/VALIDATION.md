# Validation Guide

## Before every formal test

1. Record the topology, device models, firmware, port profiles, IP plan, negotiated link rates, MTU, and enabled security/acceleration features.
2. Run the same test on a known-good same-VLAN path as the endpoint baseline.
3. Use the same duration, stream count, direction, and endpoint software for every comparison.
4. Repeat each performance condition at least three times.
5. Save NetLab Assist JSON, relevant screenshots, switch CLI output, and packet capture under the same case identifier.

## Interpreting ACL probes

- `connected`: the network path and listening service accepted TCP.
- `refused`: the target was reached but nothing accepted the port. This does not prove that an ACL permitted the intended application.
- `timeout` or `no_reply`: compatible with a drop policy, but still compare with a before-policy baseline and verify routing/ARP.
- `dns_error` or `network_error`: environment failure; do not score it as a policy pass.

## Interpreting throughput

- Evaluate forward and reverse directions separately.
- Aggregate bidirectional throughput is the sum of two independent directions, not the capacity of one direction.
- A multi-stream test does not prove LACP member distribution. Check per-member counters, SNMP, CLI, or capture.
- CPU, NIC drivers, offload, NAT, inspection features, TCP windows, and endpoint software can all be bottlenecks.

## Interpreting multicast

- Application receipt proves that the host received subscribed traffic; it does not prove that unjoined ports were free of flooded traffic.
- Verify IGMP groups and egress behavior with switch tables, counters, SNMP, or capture.
- Join/leave timestamps are application observations. Immediate-leave and querier behavior must be correlated with switch state.

## Evidence labels

Use one of these labels in a report:

- `NetLab Assist measured`: produced by this tool on the stated topology.
- `Packet capture measured`: directly supported by a saved pcap and frame numbers.
- `Device state measured`: supported by Controller UI, CLI, counters, or logs.
- `Expected behavior`: theory or a retest template, not a completed observation.
