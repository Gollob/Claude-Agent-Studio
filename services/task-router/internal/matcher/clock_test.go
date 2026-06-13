package matcher

import "time"

// nowNano returns a monotonic nanosecond timestamp for latency measurement.
func nowNano() int64 { return time.Now().UnixNano() }
