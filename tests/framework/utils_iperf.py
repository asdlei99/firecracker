# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""File containing utility methods for iperf-based performance tests"""

import concurrent.futures
import json
import time

from framework import utils
from framework.utils import CmdBuilder, CpuMap, get_cpu_percent, summarize_cpu_percent

DURATION = "duration"
IPERF3_END_RESULTS_TAG = "end"
THROUGHPUT = "throughput"
CPU_UTILIZATION_VMM = "cpu_utilization_vmm"
CPU_UTILIZATION_VCPUS_TOTAL = "cpu_utilization_vcpus_total"

# Dictionary mapping modes (guest-to-host, host-to-guest, bidirectional) to arguments passed to the iperf3 clients spawned
MODE_MAP = {"g2h": [""], "h2g": ["-R"], "bd": ["", "-R"]}

# Dictionary doing the reserve of the above, for pretty-printing
REV_MODE_MAP = {"": "g2h", "-R": "h2g"}

# Number of seconds to wait for the iperf3 server to start
SERVER_STARTUP_TIME_SEC = 2


class IPerf3Test:
    """Class abstracting away the setup and execution of an iperf3-based performance test"""

    def __init__(
        self,
        microvm,
        base_port,
        runtime,
        omit,
        mode,
        num_clients,
        connect_to,
        *,
        iperf="iperf3",
        payload_length="DEFAULT",
    ):
        self._microvm = microvm
        self._base_port = base_port
        self._runtime = runtime
        self._omit = omit
        self._mode = mode  # entry into mode-map
        self._num_clients = num_clients
        self._connect_to = connect_to  # the "host" value to pass to "--client"
        self._payload_length = payload_length  # the value to pass to "--len"
        self._iperf = iperf
        self._guest_iperf = iperf

    def run_test(self, first_free_cpu):
        """Runs the performance test, using pinning the iperf3 servers to CPUs starting from `first_free_cpu`"""
        assert self._num_clients < CpuMap.len() - self._microvm.vcpus_count - 2

        for server_idx in range(self._num_clients):
            assigned_cpu = CpuMap(first_free_cpu)
            cmd = (
                self.host_command(server_idx)
                .with_arg("--affinity", assigned_cpu)
                .build()
            )
            utils.run_cmd(f"{self._microvm.jailer.netns_cmd_prefix()} {cmd}")
            first_free_cpu += 1

        time.sleep(SERVER_STARTUP_TIME_SEC)

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = []
            cpu_load_future = executor.submit(
                get_cpu_percent,
                self._microvm.firecracker_pid,
                # Ignore the final two data points as they are impacted by test teardown
                self._runtime - 2,
                self._omit,
            )

            for client_idx in range(self._num_clients):
                futures.append(executor.submit(self.spawn_iperf3_client, client_idx))

            data = {"cpu_load_raw": cpu_load_future.result(), "g2h": [], "h2g": []}

            for i, future in enumerate(futures):
                key = REV_MODE_MAP[MODE_MAP[self._mode][i % len(MODE_MAP[self._mode])]]
                data[key].append(json.loads(future.result()))

            return data

    def host_command(self, port_offset):
        """Builds the command used for spawning an iperf3 server on the host"""
        return (
            CmdBuilder(self._iperf)
            .with_arg("-sD")
            .with_arg("-p", self._base_port + port_offset)
            .with_arg("-1")
        )

    def spawn_iperf3_client(self, client_idx):
        """
        Spawns an iperf3 client within the guest. The `client_idx` determines what direction data should flow
        for this particular client (e.g. client-to-server or server-to-client)
        """
        # Distribute modes evenly
        mode = MODE_MAP[self._mode][client_idx % len(MODE_MAP[self._mode])]

        # Add the port where the iperf3 client is going to send/receive.
        cmd = (
            self.guest_command(client_idx)
            .with_arg(mode)
            .with_arg("--affinity", client_idx % self._microvm.vcpus_count)
            .build()
        )

        rc, stdout, stderr = self._microvm.ssh.run(cmd)

        assert rc == 0, stderr

        return stdout

    def guest_command(self, port_offset):
        """Builds the command used for spawning an iperf3 client in the guest"""
        cmd = (
            CmdBuilder(self._guest_iperf)
            .with_arg("--time", self._runtime)
            .with_arg("--json")
            .with_arg("--omit", self._omit)
            .with_arg("-p", self._base_port + port_offset)
            .with_arg("--client", self._connect_to)
        )

        if self._payload_length != "DEFAULT":
            return cmd.with_arg("--len", self._payload_length)
        return cmd


def consume_iperf3_output(stats_consumer, iperf_result):
    """Consume the iperf3 data produced by the tcp/vsock throughput performance tests"""

    for iperf3_raw in iperf_result["g2h"] + iperf_result["h2g"]:
        total_received = iperf3_raw[IPERF3_END_RESULTS_TAG]["sum_received"]
        duration = float(total_received["seconds"])
        stats_consumer.consume_data(DURATION, duration)

        # Computed at the receiving end.
        total_recv_bytes = int(total_received["bytes"])
        tput = round((total_recv_bytes * 8) / (1024 * 1024 * duration), 2)
        stats_consumer.consume_data(THROUGHPUT, tput)

    vmm_util, vcpu_util = summarize_cpu_percent(iperf_result["cpu_load_raw"])

    stats_consumer.consume_stat("Avg", CPU_UTILIZATION_VMM, vmm_util)
    stats_consumer.consume_stat("Avg", CPU_UTILIZATION_VCPUS_TOTAL, vcpu_util)

    for idx, time_series in enumerate(iperf_result["g2h"]):
        yield from [
            (f"{THROUGHPUT}_g2h_{idx}", x["sum"]["bits_per_second"], "Megabits/Second")
            for x in time_series["intervals"]
        ]

    for idx, time_series in enumerate(iperf_result["h2g"]):
        yield from [
            (f"{THROUGHPUT}_h2g_{idx}", x["sum"]["bits_per_second"], "Megabits/Second")
            for x in time_series["intervals"]
        ]

    for thread_name, data in iperf_result["cpu_load_raw"].items():
        yield from [
            (f"cpu_utilization_{thread_name}", x, "Percent")
            for x in list(data.values())[0]
        ]


def emit_iperf3_metrics(metrics, iperf_result, omit):
    """Consume the iperf3 data produced by the tcp/vsock throughput performance tests"""
    for cpu_util_data_point in list(
        iperf_result["cpu_load_raw"]["firecracker"].values()
    )[0]:
        metrics.put_metric("cpu_utilization_vmm", cpu_util_data_point, "Percent")

    data_points = zip(
        *[time_series["intervals"][omit:] for time_series in iperf_result["g2h"]]
    )

    for point_in_time in data_points:
        metrics.put_metric(
            "throughput_guest_to_host",
            sum(interval["sum"]["bits_per_second"] for interval in point_in_time),
            "Bits/Second",
        )

    data_points = zip(
        *[time_series["intervals"][omit:] for time_series in iperf_result["h2g"]]
    )

    for point_in_time in data_points:
        metrics.put_metric(
            "throughput_host_to_guest",
            sum(interval["sum"]["bits_per_second"] for interval in point_in_time),
            "Bits/Second",
        )
