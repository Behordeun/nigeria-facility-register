{#
  Punctuation-normalised LGA join key: lower-case, with slashes, hyphens and
  dots collapsed to single spaces, so akoko-edo and akoko edo key the same.
  Defined once and reused, so the regex is not duplicated across the mart.
  Mirrors nfr.keys.norm_lga in the Python layer.
#}
{% macro norm_lga_key(col) %}
    regexp_replace(lower(regexp_replace({{ col }}, '[/\-\.]', ' ', 'g')), '\s+', ' ', 'g')
{% endmacro %}
