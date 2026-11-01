"""Bounded declarative validation. No evaluated expressions or user-supplied code."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Options(Strict):
    delimiter: str = ','
    decimal_separator: str = '.'
    date_format: str = 'iso'
    @model_validator(mode='after')
    def supported(self):
        if self.delimiter not in (',', ';', '\t', '|') or self.decimal_separator not in ('.', ',') or self.date_format not in ('iso', 'dmy', 'mdy'):
            raise ValueError('Choose a supported delimiter, decimal separator and date format.')
        return self

class FieldRule(Strict):
    key: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    label: str = Field(min_length=1, max_length=80)
    type: str = 'text'
    required: bool = True
    normalization: str = 'trim'
    max_length: int = Field(default=200, ge=1, le=2000)
    minimum: str | None = Field(default=None, max_length=40)
    maximum: str | None = Field(default=None, max_length=40)
    scale: int = Field(default=2, ge=0, le=6)
    allowed_values: list[str] = Field(default_factory=list, max_length=100)
    @model_validator(mode='after')
    def rules(self):
        if self.type not in ('text', 'email', 'country', 'integer', 'decimal', 'date') or self.normalization not in ('none', 'trim', 'lower', 'upper'):
            raise ValueError('Unsupported field type or normalization.')
        if any(not value or len(value) > 200 for value in self.allowed_values) or len(set(self.allowed_values)) != len(self.allowed_values):
            raise ValueError('Allowed values must be distinct, nonempty strings up to 200 characters.')
        if self.allowed_values and self.type not in ('text', 'email', 'country'):
            raise ValueError('Allowed values are supported for text/email/country fields.')
        if (self.minimum is not None or self.maximum is not None) and self.type not in ('integer', 'decimal'):
            raise ValueError('Numeric bounds require an integer or decimal field.')
        try:
            bounds = [Decimal(v) if v is not None else None for v in (self.minimum, self.maximum)]
            if any(v is not None and (not v.is_finite() or v.copy_abs() > Decimal('9000000000000')) for v in bounds):
                raise ValueError('Numeric bounds must be finite and within +/-9 trillion.')
            if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
                raise ValueError('Minimum cannot exceed maximum.')
        except InvalidOperation as exc:
            raise ValueError('Invalid numeric bound.') from exc
        return self

class Definition(Strict):
    key_field: str
    fields: list[FieldRule] = Field(min_length=1, max_length=20)
    @model_validator(mode='after')
    def identity(self):
        keys = [f.key for f in self.fields]
        if {'record_key','run_id','updated_at'} & set(keys):
            raise ValueError('Field key conflicts with reserved catalog metadata.')
        if len(set(keys)) != len(keys):
            raise ValueError('Field keys must be unique.')
        key = next((f for f in self.fields if f.key == self.key_field), None)
        if key is None or key.type != 'text' or not key.required:
            raise ValueError('The unique key must be a required text field.')
        return self

DEFAULT_OPTIONS = Options().model_dump()

def parse_definition(raw):
    return Definition.model_validate(raw).model_dump()

def parse_options(raw=None):
    return Options.model_validate(raw or {}).model_dump()

def normalized_date(value, options):
    pattern = {'iso':'%Y-%m-%d', 'dmy':'%d/%m/%Y', 'mdy':'%m/%d/%Y'}[options['date_format']]
    parsed = datetime.strptime(value, pattern).date()
    if parsed.strftime(pattern) != value:
        raise ValueError('Date must exactly match the selected format.')
    return parsed.isoformat()

def normalized_number(value, options):
    separator = options['decimal_separator']
    pattern = r'-?\d+(?:\.\d+)?' if separator == '.' else r'-?\d+(?:,\d+)?'
    if not re.fullmatch(pattern, value):
        raise ValueError('Use the selected decimal separator without grouping separators.')
    number = Decimal(value.replace(',', '.'))
    if not number.is_finite() or number.copy_abs() > Decimal('9000000000000'):
        raise ValueError('Number exceeds the supported range.')
    return number

def normalize_custom(row, mapping, definition, options):
    values, errors = {}, []
    for rule in definition['fields']:
        key = rule['key']
        raw = row.get(mapping.get(key, ''), '')
        value = '' if raw is None else str(raw)
        mode = rule['normalization']
        if mode != 'none': value = value.strip()
        if mode == 'lower': value = value.lower()
        if mode == 'upper': value = value.upper()
        if not value:
            values[key] = None
            if rule['required']: errors.append(f'{key}: required')
            continue
        values[key] = value
        try:
            if key == definition['key_field'] and len(value) > 120: raise ValueError('record key maximum 120 characters')
            if len(value) > rule['max_length']: raise ValueError(f'maximum {rule["max_length"]} characters')
            if rule['type'] == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value): raise ValueError('invalid email address')
            if rule['type'] == 'country' and not re.fullmatch(r'[A-Z]{2}', value): raise ValueError('use a two-letter uppercase country code')
            if rule['allowed_values'] and value not in rule['allowed_values']: raise ValueError('not in the configured allowed values')
            if rule['type'] == 'date': values[key] = normalized_date(value, options)
            if rule['type'] in ('integer', 'decimal'):
                n = normalized_number(value, options)
                if rule['type'] == 'integer' and (not re.fullmatch(r'-?\d+', value) or n != n.to_integral_value()): raise ValueError('use a whole number')
                if rule['type'] == 'decimal' and n.as_tuple().exponent < -rule['scale']: raise ValueError(f'at most {rule["scale"]} decimal places')
                if rule['minimum'] is not None and n < Decimal(rule['minimum']): raise ValueError('below minimum')
                if rule['maximum'] is not None and n > Decimal(rule['maximum']): raise ValueError('above maximum')
                if n == 0: n = abs(n)
                values[key] = int(n) if rule['type'] == 'integer' else format(n, f'.{rule["scale"]}f')
        except (ValueError, InvalidOperation, OverflowError) as exc:
            errors.append(f'{key}: {exc}')
    return values, errors
