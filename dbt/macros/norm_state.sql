{#
  Canonicalise a state label so v2 and v3 agree on the same state.

  v2 and v3 sometimes label the same state differently (for example the FCT
  appears as "FCT, Abuja" in one source and "FCT" in another, and Nasarawa is
  spelled "Nassarawa" in places). The authority switch and the mart joins both
  must key on one canonical form, or a covered state keeps v2 rows alongside v3
  (duplicates) and the mart shows zero for the mislabelled LGAs.

  This mirrors nfr.keys.norm_state in the Python layer. Keep the two in step.
#}
{% macro norm_state(col) %}
    case trim({{ col }})
        when 'FCT, Abuja' then 'FCT'
        when 'Fct' then 'FCT'
        when 'Fct, Abuja' then 'FCT'
        when 'Federal Capital Territory' then 'FCT'
        when 'Abuja' then 'FCT'
        when 'Nassarawa' then 'Nasarawa'
        else trim({{ col }})
    end
{% endmacro %}
