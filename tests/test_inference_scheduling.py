# SPDX-License-Identifier: Apache-2.0
"""Focused tests for the stdlib-only model execution coordinator."""

import threading
import time
import unittest

from omlx.inference_scheduling import (
    CoordinatorClosedError,
    ModelExecutionCancelledError,
    ModelExecutionCoordinator,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _wait(event):
    assert event.wait(2), "worker did not reach the expected point"


class ModelExecutionCoordinatorTests(unittest.TestCase):
    def test_disabled_path_does_not_require_registration_or_drain(self):
        coordinator = ModelExecutionCoordinator()
        with coordinator.work("unregistered") as work:
            self.assertFalse(work.checkpoint())
        self.assertIsNone(coordinator.status_snapshot()["owner"])

    def test_zero_admission_releases_idle_owner_to_positive_peer(self):
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=100)
        coordinator.register_model("paused", drain=lambda: None)
        coordinator.register_model("active", drain=lambda: None)
        coordinator.set_runnable("paused", True)
        coordinator.set_runnable("active", True)
        self.assertEqual(coordinator.status_snapshot()["owner"], "paused")
        coordinator.set_model_admission("paused", False)
        self.assertEqual(coordinator.status_snapshot()["owner"], "active")
        self.assertFalse(
            coordinator.status_snapshot()["models"]["paused"]["admission_enabled"]
        )

    def test_zero_admission_waits_before_disabled_coordinator_work_and_resumes(self):
        coordinator = ModelExecutionCoordinator(enabled=False)
        registration = coordinator.register_model("paused")
        coordinator.set_model_admission("paused", False)
        entered = threading.Event()

        def run_generation():
            with registration.work():
                entered.set()

        thread = threading.Thread(target=run_generation)
        thread.start()
        time.sleep(0.02)
        self.assertFalse(entered.is_set())
        coordinator.set_model_admission("paused", True)
        _wait(entered)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_maintenance_remains_available_when_model_admission_is_zero(self):
        coordinator = ModelExecutionCoordinator(enabled=True)
        registration = coordinator.register_model("paused")
        coordinator.set_model_admission("paused", False)
        with registration.work(kind="maintenance"):
            self.assertTrue(True)

    def test_higher_priority_handover_drains_before_next_owner_runs(self):
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=100)
        events = []
        a = coordinator.register_model("a", drain=lambda: events.append("drain-a"))
        b = coordinator.register_model(
            "b", priority=1, drain=lambda: events.append("drain-b")
        )
        coordinator.set_runnable("a", True)
        b_entered = threading.Event()

        def run_b():
            with b.work():
                events.append("b-enter")
                b_entered.set()
                coordinator.set_runnable("b", False)

        with a.work() as work_a:
            coordinator.set_runnable("b", True)
            thread = threading.Thread(target=run_b)
            thread.start()
            deadline = time.monotonic() + 2
            while coordinator.status_snapshot()["models"]["b"]["active"] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            self.assertTrue(work_a.checkpoint())
            _wait(b_entered)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertLess(events.index("drain-a"), events.index("b-enter"))
        self.assertLess(events.index("b-enter"), events.index("drain-b"))

    def test_idle_owner_yields_when_higher_priority_model_becomes_runnable(self):
        events = []
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=100)
        a = coordinator.register_model("a", drain=lambda: events.append("drain-a"))
        b = coordinator.register_model("b", priority=10, drain=lambda: None)
        coordinator.set_runnable("a", True)
        with a.work():
            pass
        coordinator.set_runnable("b", True)
        self.assertEqual(coordinator.status_snapshot()["owner"], "b")
        self.assertEqual(events, ["drain-a"])
        with b.work():
            pass

    def test_submission_reservation_prevents_drain_behind_queued_owner_work(self):
        events = []
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=0)
        a = coordinator.register_model("a", drain=lambda: events.append("drain-a"))
        b = coordinator.register_model("b", priority=10, drain=lambda: None)
        coordinator.set_runnable("a", True)
        with a.work():
            pass
        reservation = a.reserve_submission()
        self.assertIsNotNone(reservation)
        coordinator.set_runnable("b", True)
        self.assertEqual(events, [])
        b_entered = threading.Event()

        def run_b():
            with b.work():
                events.append("b-enter")
                b_entered.set()
                coordinator.set_runnable("b", False)

        peer_thread = threading.Thread(target=run_b)
        peer_thread.start()
        entered = threading.Event()

        def run_reserved_owner_work():
            with reservation.work() as work:
                events.append("a-enter")
                entered.set()
                self.assertTrue(work.checkpoint())

        owner_thread = threading.Thread(target=run_reserved_owner_work)
        owner_thread.start()
        _wait(entered)
        _wait(b_entered)
        owner_thread.join(timeout=2)
        peer_thread.join(timeout=2)
        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(peer_thread.is_alive())
        self.assertLess(events.index("a-enter"), events.index("drain-a"))
        self.assertEqual(coordinator.status_snapshot()["owner"], "a")

    def test_submission_reservation_is_denied_while_its_model_drains(self):
        drain_started = threading.Event()
        finish_drain = threading.Event()
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=0)

        def drain_a():
            drain_started.set()
            _wait(finish_drain)

        a = coordinator.register_model("a", drain=drain_a)
        b = coordinator.register_model("b", priority=1, drain=lambda: None)
        coordinator.set_runnable("a", True)
        b_entered = threading.Event()

        def run_b():
            with b.work():
                b_entered.set()
                coordinator.set_runnable("b", False)

        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        reserve_result = []

        def reserve_during_drain():
            _wait(drain_started)
            reserve_result.append(a.reserve_submission())
            finish_drain.set()

        finish_thread = threading.Thread(target=reserve_during_drain)
        with a.work() as work_a:
            coordinator.set_runnable("b", True)
            deadline = time.monotonic() + 2
            while coordinator.status_snapshot()["models"]["b"]["active"] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            finish_thread.start()
            work_a.checkpoint()
        finish_thread.join(timeout=2)
        self.assertFalse(finish_thread.is_alive())
        self.assertEqual(reserve_result, [None])
        _wait(b_entered)
        thread_b.join(timeout=2)
        self.assertFalse(thread_b.is_alive())

    def test_released_submission_allows_waiting_peer_to_drain(self):
        events = []
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=100)
        a = coordinator.register_model("a", drain=lambda: events.append("drain-a"))
        coordinator.register_model("b", priority=10, drain=lambda: None)
        coordinator.set_runnable("a", True)
        with a.work():
            pass
        reservation = a.reserve_submission()
        coordinator.set_runnable("b", True)
        self.assertEqual(events, [])
        reservation.cancel()
        self.assertEqual(events, ["drain-a"])
        self.assertEqual(coordinator.status_snapshot()["owner"], "b")

    def test_releasing_queued_reservation_preserves_active_executor_binding(self):
        coordinator = ModelExecutionCoordinator(enabled=True)
        a = coordinator.register_model("a", drain=lambda: None)
        entered = threading.Event()
        with a.work():
            reservation = a.reserve_submission()

            def run_regular_work():
                with a.work():
                    entered.set()

            thread = threading.Thread(target=run_regular_work)
            thread.start()
            time.sleep(0.03)
            self.assertFalse(entered.is_set())
            reservation.release()
            time.sleep(0.03)
            self.assertFalse(entered.is_set())
        _wait(entered)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_disabled_submission_reservation_blocks_hot_enable_until_worker_exits(self):
        coordinator = ModelExecutionCoordinator()
        a = coordinator.register_model("a", drain=lambda: None)
        b = coordinator.register_model("b", drain=lambda: None)
        reservation = a.reserve_submission()
        coordinator.configure(enabled=True)
        a_entered = threading.Event()
        allow_a_exit = threading.Event()
        b_entered = threading.Event()

        def run_a():
            with reservation.work() as work:
                a_entered.set()
                _wait(allow_a_exit)
                work.checkpoint()

        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        _wait(a_entered)

        def run_b():
            with b.work():
                b_entered.set()

        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        time.sleep(0.03)
        self.assertFalse(b_entered.is_set())
        allow_a_exit.set()
        _wait(b_entered)
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)
        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())

    def test_multiple_disabled_reservations_convert_after_hot_enable(self):
        coordinator = ModelExecutionCoordinator(quantum_seconds=0)
        a = coordinator.register_model("a", drain=lambda: None)
        b = coordinator.register_model("b", drain=lambda: None)
        reservation_a = a.reserve_submission()
        reservation_b = b.reserve_submission()
        coordinator.configure(enabled=True)
        active = 0
        active_lock = threading.Lock()
        overlaps = []
        entered = []

        def run(reservation, name):
            nonlocal active
            with reservation.work():
                with active_lock:
                    active += 1
                    if active > 1:
                        overlaps.append(name)
                    entered.append(name)
                time.sleep(0.01)
                with active_lock:
                    active -= 1

        thread_a = threading.Thread(target=run, args=(reservation_a, "a"))
        thread_b = threading.Thread(target=run, args=(reservation_b, "b"))
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)
        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())
        self.assertCountEqual(entered, ["a", "b"])
        self.assertEqual(overlaps, [])

    def test_equal_priority_rotates_only_after_quantum_with_waiting_peer(self):
        clock = FakeClock()
        coordinator = ModelExecutionCoordinator(
            enabled=True, quantum_seconds=5, clock=clock
        )
        a = coordinator.register_model("a", drain=lambda: None)
        b = coordinator.register_model("b", drain=lambda: None)
        coordinator.set_runnable("a", True)
        coordinator.set_runnable("b", True)
        b_entered = threading.Event()

        def run_b():
            with b.work():
                b_entered.set()
                coordinator.set_runnable("b", False)

        with a.work() as work_a:
            thread = threading.Thread(target=run_b)
            thread.start()
            deadline = time.monotonic() + 2
            while coordinator.status_snapshot()["models"]["b"]["active"] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            self.assertFalse(work_a.checkpoint())
            clock.advance(5)
            self.assertTrue(work_a.checkpoint())
        _wait(b_entered)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_same_model_demand_does_not_renew_turn_clock(self):
        clock = FakeClock()
        coordinator = ModelExecutionCoordinator(
            enabled=True, quantum_seconds=5, clock=clock
        )
        a = coordinator.register_model("a", drain=lambda: None)
        b = coordinator.register_model("b", drain=lambda: None)
        coordinator.set_runnable("a", True)
        coordinator.set_runnable("b", True)
        b_entered = threading.Event()

        def run_b():
            with b.work():
                b_entered.set()
                coordinator.set_runnable("b", False)

        with a.work() as work_a:
            clock.advance(4)
            with a.work():
                pass
            clock.advance(1)
            thread = threading.Thread(target=run_b)
            thread.start()
            deadline = time.monotonic() + 2
            while coordinator.status_snapshot()["models"]["b"]["active"] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            self.assertTrue(work_a.checkpoint())
        _wait(b_entered)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_drain_failure_fails_closed(self):
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=0)
        a = coordinator.register_model("a", drain=lambda: False)
        b = coordinator.register_model("b", drain=lambda: None)
        coordinator.set_runnable("a", True)
        b_errors = []

        def run_b():
            try:
                with b.work():
                    pass
            except CoordinatorClosedError as exc:
                b_errors.append(exc)

        with a.work() as work_a:
            coordinator.set_runnable("b", True)
            thread = threading.Thread(target=run_b)
            thread.start()
            deadline = time.monotonic() + 2
            while coordinator.status_snapshot()["models"]["b"]["active"] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            with self.assertRaises(CoordinatorClosedError):
                work_a.checkpoint()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(b_errors)
        self.assertTrue(coordinator.status_snapshot()["drain_failed"])

    def test_suspension_releases_owner_and_parks_until_resumed(self):
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=5)
        a = coordinator.register_model("a", drain=lambda: None)
        coordinator.set_runnable("a", True)
        resumed = threading.Event()
        coordinator.set_suspended(True)

        def run_a():
            with a.work() as work:
                work.checkpoint()
                resumed.set()

        thread = threading.Thread(target=run_a)
        thread.start()
        deadline = time.monotonic() + 2
        while coordinator.status_snapshot()["models"]["a"]["active"] == 0:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.001)
        self.assertIsNone(coordinator.status_snapshot()["owner"])
        coordinator.set_suspended(False)
        _wait(resumed)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_cancelled_wait_raises_specific_exception(self):
        coordinator = ModelExecutionCoordinator(enabled=True)
        a = coordinator.register_model("a", drain=lambda: None)
        b = coordinator.register_model("b", drain=lambda: None)
        coordinator.set_runnable("a", True)
        coordinator.set_runnable("b", True)
        stop = threading.Event()
        errors = []
        waiting = threading.Event()

        def run_b():
            try:
                with b.work(stop_event=stop):
                    pass
            except ModelExecutionCancelledError as exc:
                errors.append(exc)

        with a.work():
            thread = threading.Thread(target=run_b)
            thread.start()
            deadline = time.monotonic() + 2
            while coordinator.status_snapshot()["models"]["b"]["active"] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            waiting.set()
            _wait(waiting)
            stop.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)

    def test_hot_enable_waits_for_existing_passthrough_work(self):
        coordinator = ModelExecutionCoordinator()
        a = coordinator.register_model("a", drain=lambda: None)
        b = coordinator.register_model("b", drain=lambda: None)
        a_entered = threading.Event()
        allow_a_exit = threading.Event()
        b_entered = threading.Event()

        def run_a():
            with a.work():
                a_entered.set()
                _wait(allow_a_exit)

        def run_b():
            with b.work():
                b_entered.set()

        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        _wait(a_entered)
        coordinator.configure(enabled=True)
        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        time.sleep(0.03)
        self.assertFalse(b_entered.is_set())
        allow_a_exit.set()
        _wait(b_entered)
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)
        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())

    def test_reenable_waits_for_work_that_became_passthrough_while_active(self):
        coordinator = ModelExecutionCoordinator(enabled=True)
        a = coordinator.register_model("a", drain=lambda: None)
        b = coordinator.register_model("b", drain=lambda: None)
        a_entered = threading.Event()
        allow_a_exit = threading.Event()
        b_entered = threading.Event()

        def run_a():
            with a.work():
                a_entered.set()
                _wait(allow_a_exit)

        def run_b():
            with b.work():
                b_entered.set()

        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        _wait(a_entered)
        coordinator.configure(enabled=False)
        coordinator.configure(enabled=True)
        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        time.sleep(0.03)
        self.assertFalse(b_entered.is_set())
        allow_a_exit.set()
        _wait(b_entered)
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)
        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())

    def test_cancellation_during_drain_is_observed_before_resume(self):
        drain_started = threading.Event()
        finish_drain = threading.Event()
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=0)

        def drain_a():
            drain_started.set()
            _wait(finish_drain)

        a = coordinator.register_model("a", drain=drain_a)
        b = coordinator.register_model("b", drain=lambda: None)
        coordinator.set_runnable("a", True)
        request_cancelled = threading.Event()
        b_entered = threading.Event()

        def run_b():
            with b.work():
                b_entered.set()
                coordinator.set_runnable("b", False)

        def cancel_during_drain():
            _wait(drain_started)
            request_cancelled.set()
            finish_drain.set()

        canceller = threading.Thread(target=cancel_during_drain)
        canceller.start()
        thread_b = threading.Thread(target=run_b)
        with a.work() as work_a:
            coordinator.set_runnable("b", True)
            thread_b.start()
            deadline = time.monotonic() + 2
            while coordinator.status_snapshot()["models"]["b"]["active"] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            with self.assertRaises(ModelExecutionCancelledError):
                work_a.checkpoint(cancelled=request_cancelled.is_set)
        canceller.join(timeout=2)
        thread_b.join(timeout=2)
        self.assertFalse(canceller.is_alive())
        self.assertFalse(thread_b.is_alive())
        self.assertTrue(b_entered.is_set())

    def test_invalid_global_configuration_is_rejected_without_mutation(self):
        with self.assertRaises(TypeError):
            ModelExecutionCoordinator(enabled=1)
        with self.assertRaises(ValueError):
            ModelExecutionCoordinator(quantum_seconds=float("inf"))
        coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=5)
        before = coordinator.status_snapshot()
        with self.assertRaises(TypeError):
            coordinator.configure(enabled=1)
        with self.assertRaises(ValueError):
            coordinator.configure(quantum_seconds=float("nan"))
        self.assertEqual(coordinator.status_snapshot(), before)

    def test_maintenance_work_can_run_during_suspension(self):
        coordinator = ModelExecutionCoordinator(enabled=True)
        model = coordinator.register_model("a", drain=lambda: None)
        coordinator.set_suspended(True)
        entered = threading.Event()

        def run_maintenance():
            with model.work(kind="maintenance"):
                entered.set()

        thread = threading.Thread(target=run_maintenance)
        thread.start()
        _wait(entered)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
