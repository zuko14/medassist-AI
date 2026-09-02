-- Rollback 070: drop the provider-routing keys. Every report then goes to the
-- patient's own number again, which is the pre-070 behaviour.
UPDATE integration_connectors
SET config = (config - 'report_routing_providers') - 'report_routing_phone',
    updated_at = now()
WHERE connector_type = 'mocdoc'
  AND config ? 'report_routing_phone';
