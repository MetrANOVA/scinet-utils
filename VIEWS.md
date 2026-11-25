CREATE VIEW data_interface_traffic_view AS 
SELECT
  observation_time,
  device_id,
  device_ref,
  interface_id,
  interface_ref,
  interface_name,
  interface_description,
  in_bits_delta,
  out_bits_delta
FROM (
  SELECT
    observation_time,
    id AS device_id,
    ref AS device_ref,
    meta_interface.id AS interface_id,
    meta_interface.ref AS interface_ref,
    meta_interface.name AS interface_name,
    meta_interface.description AS interface_description,
    MAX(IF(metric_name = 'SNMP_IF-MIB::ifHCInOctets',  metric_value, NULL)) AS in_bits,
    MAX(IF(metric_name = 'SNMP_IF-MIB::ifHCOutOctets', metric_value, NULL)) AS out_bits,
    lag(in_bits)  OVER (PARTITION BY interface_id ORDER BY observation_time ASC) AS prev_in_bits,
    lag(out_bits) OVER (PARTITION BY interface_id ORDER BY observation_time ASC) AS prev_out_bits,
    (in_bits  - prev_in_bits) * 8  AS in_bits_delta,
    (out_bits - prev_out_bits) * 8 AS out_bits_delta
  FROM data_snmp_counter
  INNER JOIN meta_interface ON cast(data_snmp_counter.ext.oidIndex AS UInt32) = meta_interface.flow_index
  WHERE
    id IN ({ids:Array(String)})
    AND observation_time BETWEEN {from:DateTime} AND {to:DateTime}
    AND metric_name IN ('SNMP_IF-MIB::ifHCInOctets', 'SNMP_IF-MIB::ifHCOutOctets')
  GROUP BY
    observation_time,
    device_id,
    device_ref,
    interface_id,
    interface_ref,
    interface_name,
    interface_description
  ORDER BY
    observation_time ASC
  SETTINGS
    allow_suspicious_types_in_group_by = 1
)
