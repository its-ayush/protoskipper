# SCL Schema Files

This directory is the intended location for IEC 61850-6 XSD schema files
used by the optional XSD-level validation path.

## Obtaining the schemas

The official IEC 61850-6 XSD schemas are published by IEC as part of the
standard documents.  They are also distributed (without modification) by
several open-source projects:

| Edition | File | Source |
|---------|------|--------|
| 2.1 | `SCL_Schema_Ed2.1_2020.xsd` | IEC TC57 WG10 / libiec61850 |
| 2.0 | `SCL_Schema_Ed2_2007.xsd`   | IEC TC57 WG10 / libiec61850 |
| 1.0 | `SCL_Schema_Ed1_2004.xsd`   | IEC TC57 WG10               |

Place the downloaded XSD files in this directory.  The parser will pick
them up automatically when `validate(..., schema_path="...")` is called.

## libiec61850

The libiec61850 project (https://github.com/mz-automation/libiec61850)
ships the Edition 2.0 schema under `config/`.  That schema is MIT-licensed
for distribution purposes when used with libiec61850.

## Note on copyright

The IEC 61850-6 standard text and normative XSD schemas are copyright
IEC.  Redistribution of the raw schema files requires an IEC licence.
ProtoSkipper therefore does **not** bundle the official schemas; users
must obtain them separately.
