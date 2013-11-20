#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Routines for reading Meraxes output files."""

import numpy as np
import h5py as h5
from astropy import log

__author__ = 'Simon Mutch'
__email__ = 'smutch.astro@gmail.com'
__version__ = '0.1.0'


def read_gals(fname, snapshot=None, props=None, quiet=False, sim_props=False,
              pandas=False):

    """ Read in a Meraxes hdf5 output file.

    Reads in the default type of HDF5 file generated by the code.

    *Args*:
        fname (str): Full path to input hdf5 master file.

    *Kwargs*:
        snapshot (int): The snapshot to read in.
                        (default: last present snapshot - usually z=0)

        props (list): A list of galaxy properties requested.
                      (default: All properties)

        quiet (bool): Suppress output info and status messages.
                      (default: False)

        sim_props (bool): Output some simulation properties as well.
                          (default = False)

        pandas (bool): Ouput a pandas dataframe instead of a numpy array.
                       (default = False)

    *Returns*:
        Array with the requested galaxies and properties.

        If sim_props==True then output is a tuple of form
        (galaxies, sim_props) where sim_props holds the following information
        as a dictionary:

            ( BoxSize,
            MaxTreeFiles,
            ObsHubble_h,
            Volume,
            Redshift )
    """

    if pandas:
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("The pandas package must be available if"
                              " pandas=True.")

    # Open the file for reading
    fin = h5.File(fname, 'r')

    # Set the snapshot correctly
    if snapshot is None:
        present_snaps = np.asarray(sorted(fin.keys()))
        selection = [(p.find('Snap') == 0) for p in present_snaps]
        present_snaps = present_snaps[selection]
        snapshot = int(present_snaps[-1][4:])
    elif snapshot < 0:
        MaxSnaps = fin['InputParams'].attrs['LastSnapshotNr'][0]+1
        snapshot += MaxSnaps

    if not quiet:
        log.info("Reading snapshot %d" % snapshot)

    # Select the group for the requested snapshot.
    snap_group = fin['Snap%03d' % (snapshot)]

    # Create a dataset large enough to hold all of the requested galaxies
    ngals = snap_group['Galaxies'].size
    if props is not None:
        gal_dtype = snap_group['Galaxies'].value[list(props)[:]][0].dtype
    else:
        gal_dtype = snap_group['Galaxies'].dtype

    G = np.empty(ngals, dtype=gal_dtype)
    if not quiet:
        log.info("Allocated %.1f MB" % (G.itemsize*ngals/1024./1024.))

    # Loop through each of the requested groups and read in the galaxies
    if ngals > 0:
        snap_group['Galaxies'].read_direct(G, dest_sel=np.s_[:ngals])

    # Print some checking statistics
    if not quiet:
        log.info('Read in %d galaxies.' % len(G))

    # If requested convert the numpy array into a pandas dataframe
    if pandas:

        # Get a list of all of the columns which a 1D
        names = []
        for k, v in G.dtype.fields.iteritems():
            if len(v[0].shape) == 0:
                names.append(k)

        # Create a new dataframe with these columns
        Gdf = pd.DataFrame(G[names])

        # Loop through each N(>1)D galaxy property and append each dimension as
        # its own column in the dataframe
        for k, v in G.dtype.fields.iteritems():
            if len(v[0].shape) != 0:
                for i in range(v[0].shape[0]):
                    Gdf[k+'_%d' % i] = G[k][:, i]

        # Make G now point to our pandas dataframe
        G = Gdf

    # Set some run properties
    if sim_props:
        Hubble_h = fin['InputParams'].attrs['Hubble_h'][0]
        BoxSize = fin['InputParams'].attrs['BoxSize'][0] / Hubble_h
        MaxTreeFiles = fin['InputParams'].attrs['FilesPerSnapshot'][0]
        Volume = BoxSize**3.0
        Redshift = snap_group.attrs['Redshift']
        properties = {'BoxSize': BoxSize,
                      'MaxTreeFiles': MaxTreeFiles,
                      'Hubble_h': Hubble_h,
                      'Volume': Volume,
                      'Redshift': Redshift}

    fin.close()

    if sim_props:
        return G, properties
    else:
        return G


def read_input_params(fname, props=None):
    """ Read in the input parameters from a Meraxes hdf5 output file.

    Reads in the default type of HDF5 file generated by the code.

    *Args*:
        fname (str): Full path to input hdf5 master file.

    *Kwargs*:
        props (list): A list of run properties requested.
                      (default: all properties)

    *Returns*:
        A dict with the requested run properties.
    """

    # Initialise the output dictionary
    props_dict = {}

    # Open the file for reading
    fin = h5.File(fname, 'r')

    group = fin['InputParams']

    if props is None:
        props = group.attrs.keys()

        # Add some extra properties
        props_dict['Hubble_h'] = group.attrs['Hubble_h'][0]
        props_dict['BoxSize'] = group.attrs['BoxSize'][0] /\
            props_dict['Hubble_h']
        props_dict['VolumeFactor'] = group.attrs['VolumeFactor'][0]
        props_dict['Volume'] = props_dict['BoxSize']**3.0 *\
            props_dict['VolumeFactor']

    for p in props:
        try:
            props_dict[p] = group.attrs[p][0]
        except (KeyError):
            log.error("Property '%s' doesn't exist in the InputParams group." %
                      p)

    fin.close()

    return props_dict


def read_gitref(fname):
    """Read the git ref saved in the master file.

    *Args*:
        fname (str):  Full path to input hdf5 master file.

    *Returns*:
        (str) git ref of the model
    """

    with h5.File(fname, 'r') as fin:
        gitref = fin.attrs['GitRef'].copy()[0]

    return gitref


def read_snaplist(fname):

    """ Read in the list of available snapshots from the Meraxes hdf5 file.

    *Args*:
        fname (str): Full path to input hdf5 master file.

    *Returns*:
        snaps:      array of snapshots

        redshifts:  array of redshifts

        lt_times:   array of light travel times (Gyr)
    """

    zlist = []
    snaplist = []
    lt_times = []

    with h5.File(fname, 'r') as fin:
        for snap in fin.keys():
            try:
                zlist.append(fin[snap].attrs['Redshift'][0])
                snaplist.append(int(snap[-3:]))
                lt_times.append(fin[snap].attrs['LTTime'][0])
            except KeyError:
                pass

    return np.array(snaplist, dtype=float), np.array(zlist, dtype=float),\
        np.array(lt_times, dtype=float)


def grab_redshift(fname, snapshot):

    """ Quickly grab the redshift value of a single snapshot from a Meraxes
    HDF5 file.

    *Args*:
        fname (str):  Full path to input hdf5 master file

        snapshot (int):  Snapshot for which the redshift is to grabbed

    *Returns*:
        redshift (float):   Corresponding redshift value
    """

    with h5.File(fname, 'r') as fin:
        redshift = fin["Snap{:03d}".format(snapshot)].attrs["Redshift"][0]

    return redshift


def grab_corrected_snapshot(fname, snapshot):

    """ Quickly grab the corrected snapshot value of a single snapshot from a
    Meraxes HDF5 file.

    *Args*:
        fname (str):  Full path to input hdf5 master file

        snapshot (int):  Snapshot for which the corrected value is to be
                         grabbed

    *Returns*:
        redshift (float):   Corresponding corrected snapshot value
    """

    with h5.File(fname, 'r') as fin:
        redshift = fin["Snap{:03d}".format(snapshot)].attrs["CorrectedSnap"][0]

    return redshift


def read_firstprogenitor_indices(fname, snapshot):

    """ Read the FirstProgenitor indices from the Meraxes HDF5 file.

    *Args*:
        fname (str):  Full path to input hdf5 master file

        snapshot (int):  Snapshot from which the progenitors dataset is to be
                         read from.

    *Returns*:
        fp_ind (array): FirstProgenitor indices
    """

    with h5.File(fname, 'r') as fin:
        fp_ind = fin["Snap{:03d}/FirstProgenitorIndices".format(snapshot)][:]

    return fp_ind


def read_nextprogenitor_indices(fname, snapshot):

    """ Read the NextProgenitor indices from the Meraxes HDF5 file.

    *Args*:
        fname (str):  Full path to input hdf5 master file

        snapshot (int):  Snapshot from which the progenitors dataset is to be
                         read from.

    *Returns*:
        np_ind: NextProgenitor indices
    """

    with h5.File(fname, 'r') as fin:
        np_ind = fin["Snap{:03d}/NextProgenitorIndices".format(snapshot)][:]

    return np_ind


def read_xH_grid(fname, snapshot):

    """ Read the neutral hydrogren fraction (xH) grids from the Meraxes HDF5
    file.

    *Args*:
        fname (str):  Full path to input hdf5 master file

        snapshot (int):  Snapshot from which the xH grid dataset is to be
                         read from.

    *Returns*:
        xH_grid (array):   xH grid
        props (dict):   associated attributes
    """

    with h5.File(fname, 'r') as fin:
        ds_name = "Snap{:03d}/xH_grid".format(snapshot)
        xH_grid = fin[ds_name][:]
        props = dict(fin[ds_name].attrs.iteritems())

    xH_grid.shape = [props["HII_dim"][0], ]*3

    return xH_grid, props
