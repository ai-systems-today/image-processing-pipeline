# flake8: noqa
import os
import logging
from datetime import datetime
from collections import OrderedDict

import numpy as np
import tensorflow as tf

import nrrd
from nrrd.errors import NRRDError
from nrrd.writer import (
    _get_field_type, _format_field_value, _write_data, _TYPEMAP_NUMPY2NRRD,
    _NUMPY2NRRD_ENDIAN_MAP, _NRRD_FIELD_ORDER
)

from utils import open_file

log = logging.getLogger()


def load(filename):
    log.info('Loading nrrd slice {}'.format(filename))
    with open_file(filename) as fh:
        header = nrrd.read_header(fh)
        return nrrd.read_data(header, fh, filename), header


# def extract(filename, fileobj):
#     header = nrrd.read_header(fileobj)
#     spacing = np.diag(header['space directions'])
#     metadata = {
#         'space_origin': header['space origin'].tolist(),
#         'spacing': spacing[:-1].tolist(),
#         'slice_thinkness': float(spacing[-1]),
#     }
#     return nrrd.read_data(header, fileobj, filename), metadata


# def load(filename):
#     log.info('Loading nrrd slice {}'.format(filename))
#     with open_file(filename) as fh:
#         data, header = extract(filename, fh)
#     # log.error('File cloising info: {}'.format(fh))
#     return data, header


def write(
    filename,
    data,
    header=None,
    detached_header=False,
    relative_data_path=True,
    custom_field_map=None,
    compression_level=9,
    index_order='F'
):
    if header is None:
        header = {}

    # Infer a number of fields from the NumPy array and overwrite values in the header dictionary.
    # Get type string identifier from the NumPy datatype
    header['type'] = _TYPEMAP_NUMPY2NRRD[data.dtype.str[1:]]

    # If the datatype contains more than one byte and the encoding is not ASCII, then set the endian header value
    # based on the datatype's endianness. Otherwise, delete the endian field from the header if present
    if data.dtype.itemsize > 1 and header.get('encoding', '').lower() not in [
        'ascii', 'text', 'txt'
    ]:
        header['endian'] = _NUMPY2NRRD_ENDIAN_MAP[data.dtype.str[:1]]
    elif 'endian' in header:
        del header['endian']

    # If space is specified in the header, then space dimension can not. See
    # http://teem.sourceforge.net/nrrd/format.html#space
    if 'space' in header.keys() and 'space dimension' in header.keys():
        del header['space dimension']

    # Update the dimension and sizes fields in the header based on the data. Since NRRD expects meta data to be in
    # Fortran order we are required to reverse the shape in the case of the array being in C order. E.g., data was read
    # using index_order='C'.
    header['dimension'] = data.ndim
    header['sizes'] = list(data.shape
                          ) if index_order == 'F' else list(data.shape[::-1])

    # The default encoding is 'gzip'
    if 'encoding' not in header:
        header['encoding'] = 'gzip'

    # A bit of magic in handling options here.
    # If *.nhdr filename provided, this overrides `detached_header=False`
    # If *.nrrd filename provided AND detached_header=True, separate header and data files written.
    # If detached_header=True and data file is present, then write the files separately
    # For all other cases, header & data written to same file.
    if filename.endswith('.nhdr'):
        detached_header = True

        if 'data file' not in header:
            # Get the base filename without the extension
            base_filename = os.path.splitext(filename)[0]

            # Get the appropriate data filename based on encoding, see here for information on the standard detached
            # filename: http://teem.sourceforge.net/nrrd/format.html#encoding
            if header['encoding'] == 'raw':
                data_filename = '%s.raw' % base_filename
            elif header['encoding'] in ['ASCII', 'ascii', 'text', 'txt']:
                data_filename = '%s.txt' % base_filename
            elif header['encoding'] in ['gzip', 'gz']:
                data_filename = '%s.raw.gz' % base_filename
            elif header['encoding'] in ['bzip2', 'bz2']:
                data_filename = '%s.raw.bz2' % base_filename
            else:
                raise NRRDError(
                    'Invalid encoding specification while writing NRRD file: %s'
                    % header['encoding']
                )

            header['data file'] = os.path.basename(data_filename) \
                if relative_data_path else os.path.abspath(data_filename)
        else:
            # TODO This will cause issues for relative data files because it will not save in the correct spot
            data_filename = header['data file']
    elif filename.endswith('.nrrd') and detached_header:
        data_filename = filename
        header['data file'] = os.path.basename(data_filename) \
            if relative_data_path else os.path.abspath(data_filename)
        filename = '%s.nhdr' % os.path.splitext(filename)[0]
    else:
        # Write header & data as one file
        data_filename = filename
        detached_header = False

    with tf.gfile.GFile(filename, 'wb') as fh:
        fh.write(b'NRRD0005\n')
        fh.write(b'# This NRRD file was generated by pynrrd\n')
        fh.write(
            b'# on ' +
            datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S').encode('ascii') +
            b'(GMT).\n'
        )
        fh.write(b'# Complete NRRD file format specification at:\n')
        fh.write(b'# http://teem.sourceforge.net/nrrd/format.html\n')

        # Copy the options since dictionaries are mutable when passed as an argument
        # Thus, to prevent changes to the actual options, a copy is made
        # Empty ordered_options list is made (will be converted into dictionary)
        local_options = header.copy()
        ordered_options = []

        # Loop through field order and add the key/value if present
        # Remove the key/value from the local options so that we know not to add it again
        for field in _NRRD_FIELD_ORDER:
            if field in local_options:
                ordered_options.append((field, local_options[field]))
                del local_options[field]

        # Leftover items are assumed to be the custom field/value options
        # So get current size and any items past this index will be a custom value
        custom_field_start_index = len(ordered_options)

        # Add the leftover items to the end of the list and convert the options into a dictionary
        ordered_options.extend(local_options.items())
        ordered_options = OrderedDict(ordered_options)

        for x, (field, value) in enumerate(ordered_options.items()):
            # Get the field_type based on field and then get corresponding
            # value as a str using _format_field_value
            field_type = _get_field_type(field, custom_field_map)
            value_str = _format_field_value(value, field_type)

            # Custom fields are written as key/value pairs with a := instead of : delimeter
            if x >= custom_field_start_index:
                fh.write(('%s:=%s\n' % (field, value_str)).encode('ascii'))
            else:
                fh.write(('%s: %s\n' % (field, value_str)).encode('ascii'))

        # Write the closing extra newline
        fh.write(b'\n')

        # If header & data in the same file is desired, write data in the file
        if not detached_header:
            _write_data(
                data,
                fh,
                header,
                compression_level=compression_level,
                index_order=index_order
            )

    # If detached header desired, write data to different file
    if detached_header:
        with tf.gfile.GFile(data_filename, 'wb') as data_fh:
            _write_data(
                data,
                data_fh,
                header,
                compression_level=compression_level,
                index_order=index_order
            )
