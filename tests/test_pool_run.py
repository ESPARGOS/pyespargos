import queue
import threading
import time

import pytest

from espargos.pool import Pool


def make_pool_runner():
    pool = Pool.__new__(Pool)
    pool._input_queue = queue.Queue()
    pool._run_lock = threading.Lock()

    handled = []
    cluster_checks = []
    pool._handle_packet = handled.append
    pool._check_ota_clusters = lambda: cluster_checks.append(True)
    return pool, handled, cluster_checks


def test_run_processes_all_pending_messages_as_one_batch():
    pool, handled, cluster_checks = make_pool_runner()
    for packet in ("first", "second", "third"):
        pool._input_queue.put(packet)

    assert pool.run(timeout=0) == 3
    assert handled == ["first", "second", "third"]
    assert cluster_checks == [True]
    assert pool._input_queue.empty()


def test_run_leaves_messages_arriving_during_processing_for_next_batch():
    pool, handled, cluster_checks = make_pool_runner()
    pool._input_queue.put("first")

    def handle_packet(packet):
        handled.append(packet)
        if packet == "first":
            pool._input_queue.put("next")

    pool._handle_packet = handle_packet

    assert pool.run(timeout=0) == 1
    assert handled == ["first"]
    assert pool.run(timeout=0) == 1
    assert handled == ["first", "next"]
    assert cluster_checks == [True, True]


def test_run_can_wait_for_the_first_message():
    pool, handled, _cluster_checks = make_pool_runner()

    def produce():
        time.sleep(0.02)
        pool._input_queue.put("delayed")

    producer = threading.Thread(target=produce)
    producer.start()
    try:
        assert pool.run(timeout=0.5) == 1
    finally:
        producer.join()

    assert handled == ["delayed"]


def test_run_timeout_still_checks_cluster_expiration():
    pool, handled, cluster_checks = make_pool_runner()

    started = time.monotonic()
    assert pool.run(timeout=0.01) == 0
    elapsed = time.monotonic() - started

    assert handled == []
    assert cluster_checks == [True]
    assert elapsed >= 0.005


def test_run_rejects_concurrent_consumers():
    pool, handled, _cluster_checks = make_pool_runner()
    result = []

    def consume():
        result.append(pool.run(timeout=0.5))

    consumer = threading.Thread(target=consume)
    consumer.start()
    while not pool._run_lock.locked():
        time.sleep(0.001)

    with pytest.raises(RuntimeError, match="concurrently or reentrantly"):
        pool.run(timeout=0)

    pool._input_queue.put("release")
    consumer.join()

    assert result == [1]
    assert handled == ["release"]


def test_run_rejects_negative_timeout():
    pool, _handled, _cluster_checks = make_pool_runner()

    with pytest.raises(ValueError, match="non-negative"):
        pool.run(timeout=-0.001)
