import time


class CircuitBreakerOpen(Exception):
    pass


class CircuitBreaker:

    def __init__(
        self,
        failure_threshold=3,
        recovery_timeout=10
    ):

        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self.failure_count = 0
        self.state = "CLOSED"
        self.last_failure_time = None

    def allow_request(self):

        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":

            elapsed_time = (
                time.time() -
                self.last_failure_time
            )

            if elapsed_time >= self.recovery_timeout:
                self.state = "HALF_OPEN"
                return True

            raise CircuitBreakerOpen(
                "Circuit Breaker is OPEN"
            )

        return True

    def record_success(self):

        self.failure_count = 0
        self.state = "CLOSED"
        self.last_failure_time = None

    def record_failure(self):

        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == "HALF_OPEN":
            self.state = "OPEN"
        return

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"

    def get_state(self):
        return self.state