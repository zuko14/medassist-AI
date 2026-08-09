-- Migration 023: Atomic rate-limit check-and-increment RPC
-- Run in Supabase SQL Editor
--
-- PersistentRateLimiter.is_rate_limited() + .record_attempt() were two
-- separate round trips (read-then-write), so parallelized concurrent login
-- attempts could all pass is_rate_limited() before any of their
-- record_attempt() writes landed — allowing more than max_attempts guesses
-- per window. This function makes check + increment one atomic statement.

CREATE OR REPLACE FUNCTION check_and_record_rate_limit(
    p_key TEXT,
    p_max_attempts INT,
    p_window_seconds INT
) RETURNS INT AS $$
DECLARE
    v_attempts INT;
BEGIN
    INSERT INTO rate_limits (key, attempts, window_start)
    VALUES (p_key, 1, now())
    ON CONFLICT (key) DO UPDATE SET
        attempts = CASE
            WHEN rate_limits.window_start >= now() - (p_window_seconds || ' seconds')::interval
                THEN rate_limits.attempts + 1
            ELSE 1
        END,
        window_start = CASE
            WHEN rate_limits.window_start >= now() - (p_window_seconds || ' seconds')::interval
                THEN rate_limits.window_start
            ELSE now()
        END
    RETURNING attempts INTO v_attempts;

    RETURN v_attempts;
END;
$$ LANGUAGE plpgsql;

-- Verify
SELECT proname FROM pg_proc WHERE proname = 'check_and_record_rate_limit';
