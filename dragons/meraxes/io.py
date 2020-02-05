#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Routines for reading Meraxes output files."""

from ..munge import ndarray_to_dataframe

import re
import numpy as np
import h5py as h5
from astropy.table import Table
import pandas as pd
import logging


__meraxes_h = None
logger = logging.getLogger(__name__)
logger.setLevel('WARNING')


def _check_pandas():
    try:
        pd
    except NameError:
        raise ImportError("The pandas package must be available if" " pandas=True.")


def set_little_h(h=None):

    """ Set the value of little h to be used by all future meraxes.io calls
    where applicable.

    Parameters
    ----------
    h : float or str
        Little h value.  If a filename is passed as a string, then little h
        will be set to the simulation value read from that file.
        (default: None)

    Returns
    -------
    h : float
        Little h value.
    """

    if type(h) is str or type(h) is str:
        h = read_input_params(h)["Hubble_h"]

    global __meraxes_h

    logger.info("Setting little h to %.3f for future io calls." % h)

    if h == 1.0:
        h = None

    __meraxes_h = h

    return h


def read_gals(
    fname, snapshot=None, props=None, sim_props=False, pandas=False, table=False, h=None, indices=None
):

    """Read in a Meraxes hdf5 output file.

    Reads in the default type of HDF5 file generated by the code.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file.

    snapshot : int
        The snapshot to read in.  (default: last present snapshot - usually
        z=0)

    props : list
        A list of galaxy properties requested.  (default: All properties)

    sim_props : bool
        Output some simulation properties as well.  (default = False)

    pandas : bool
        Ouput a pandas DataFrame instead of an astropy table.  (default =
        False)

    table : bool
        Output an astropy Table instead of a numpy ndarray.  (default =
        False)

    h : float
        Hubble constant (/100) to scale the galaxy properties to.  If
        `None` then no scaling is made unless `set_little_h` was previously
        called.  (default = None)

    indices : list or array
        Indices of galaxies to be read.  If `None` then read all galaxies.
        (default = None)

    Returns
    -------
        An ndarray with the requested galaxies and properties.

        If sim_props==True then output is a tuple of form (galaxies, sim_props)
    """

    if (h is None) and (__meraxes_h is not None):
        h = __meraxes_h

    def __apply_offsets(G, dest_sel, counter):
        # Deal with any indices that need offsets applied
        try:
            G[dest_sel]["CentralGal"] += counter
        except ValueError:
            pass

    if pandas:
        _check_pandas()

    if pandas and table:
        logger.error("Both `pandas` and `table` specified.  Please choose one" " or the other.")

    # Grab the units and hubble conversions information
    units = read_units(fname)

    # Open the file for reading
    fin = h5.File(fname, "r")

    # Set the snapshot correctly
    if snapshot is None:
        snapshot = -1
    if snapshot < 0:
        present_snaps = np.asarray(list(fin.keys()))
        selection = np.array([(p.find("Snap") == 0) for p in present_snaps])
        present_snaps = [int(p[4:]) for p in present_snaps[selection]]
        snapshot = sorted(present_snaps)[snapshot]

    logger.info("Reading snapshot %d" % snapshot)

    # Select the group for the requested snapshot.
    snap_group = fin["Snap%03d" % (snapshot)]

    # How many cores have been used for this run?
    n_cores = fin.attrs["NCores"][0]

    # Grab the total number of galaxies in this snapshot
    ngals = snap_group.attrs["NGalaxies"][0]

    if ngals == 0:
        raise IndexError("There are no galaxies in snapshot {:d}!".format(snapshot))

    # Reset ngals to be the number of requested galaxies if appropriate
    if indices is not None:
        indices = np.array(indices, "i")
        indices.sort()
        ngals = indices.shape[0]

    # Set the galaxy data type
    gal_dtype = None
    for i_core in range(n_cores):
        try:
            if props is not None:
                gal_dtype = snap_group["Core%d/Galaxies" % i_core][tuple(props)][0].dtype
            else:
                gal_dtype = snap_group["Core%d/Galaxies" % i_core].dtype
        except IndexError:
            pass
        if gal_dtype is not None:
            break

    # Newer versions of numpy will return a dtype with no fields if we have
    # only requested one property.  We need to reconstructed a named dtype for
    # the direct read below.
    if gal_dtype.names is None:
        assert len(props) == 1
        gal_dtype = np.dtype(list(zip(props, [gal_dtype.type])))

    # Create a dataset large enough to hold all of the requested galaxies
    G = np.empty(ngals, dtype=gal_dtype)
    logger.info("Allocated %.1f MB" % (G.itemsize * ngals / 1024.0 / 1024.0))

    # Loop through each of the requested groups and read in the galaxies
    if ngals > 0:
        counter = 0
        total_read = 0
        for i_core in range(n_cores):
            galaxies = snap_group["Core%d/Galaxies" % i_core]
            core_ngals = galaxies.size

            if core_ngals > 0:
                if indices is None:
                    dest_sel = np.s_[counter : core_ngals + counter]
                    galaxies.read_direct(G, dest_sel=dest_sel)

                    __apply_offsets(G, dest_sel, counter)
                    counter += core_ngals

                else:
                    read_ind = (
                        np.compress((indices >= total_read) & (indices < total_read + core_ngals), indices) - total_read
                    )

                    if read_ind.shape[0] > 0:
                        dest_sel = np.s_[counter : read_ind.shape[0] + counter]
                        bool_sel = np.zeros(core_ngals, "bool")
                        bool_sel[read_ind] = True
                        G[dest_sel] = galaxies[G.dtype.names][bool_sel]

                        __apply_offsets(G, dest_sel, total_read)
                        counter += read_ind.shape[0]

                    total_read += core_ngals

            if counter >= ngals:
                break

    # Print some checking statistics
    logger.info("Read in %d galaxies." % len(G))

    # Apply any Hubble scalings
    if h is not None:
        h = float(h)
        h_conv = units["HubbleConversions"]
        logger.info("Scaling galaxy properties to h = %.3f" % h)
        for p in gal_dtype.names:
            try:
                conversion = h_conv[p]
            except KeyError:
                logger.warn("Unrecognised galaxy property %s - assuming no " "scaling with Hubble const!" % p)
            if conversion.lower() != "none":
                try:
                    G[p] = eval(conversion, dict(v=G[p], h=h, log10=np.log10, __builtins__={}))
                except:
                    logger.error("Failed to parse conversion string `%s` for unit" " %s" % (conversion, p))

    # If requested convert the numpy array into a pandas dataframe
    if pandas:
        logger.info("Converting to pandas DataFrame...")
        G = ndarray_to_dataframe(G)
        regex = re.compile("_\d*$")
        # attach the units to each column
        for k in G.columns:
            try:
                G[k].unit = units[re.sub(regex, "", k, 1)]
            except KeyError:
                logger.warn("Unrecognised galaxy property %s - assuming " "dimensionless quantitiy!" % k)
    # else convert to astropy table and attach units
    elif table:
        logger.info("Converting to astropy Table...")
        G = Table(G, copy=False)
        for k, v in G.columns.items():
            try:
                v.unit = units[k]
            except KeyError:
                logger.warn("Unrecognised galaxy property %s - assuming " "dimensionless quantitiy!" % k)

    # Set some run properties
    if sim_props:
        properties = read_input_params(fname, h=h)
        properties["Redshift"] = snap_group.attrs["Redshift"]

    fin.close()

    if sim_props:
        return G, properties
    else:
        return G


def read_input_params(fname, h=None, raw=False):
    """ Read in the input parameters from a Meraxes hdf5 output file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file.

    h : float
        Hubble constant (/100) to scale the galaxy properties to.  If
        `None` then no scaling is made unless `set_little_h` was previously
        called.  (default = None)

    raw : bool
        Don't augment with extra useful quantities. (default = False)

    Returns
    -------
    dict
        All run properties.
    """

    if (h is None) and (__meraxes_h is not None):
        h = __meraxes_h

    def arr_to_value(d):
        for k, v in list(d.items()):
            if isinstance(v, np.bytes_):
                d[k] = str(v.astype(np.str_))
            elif v.size == 1:
                try:
                    d[k] = v[0]
                except IndexError:
                    d[k] = v

    def visitfunc(name, obj):
        if isinstance(obj, h5.Group):
            props_dict[name] = dict(list(obj.attrs.items()))
            arr_to_value(props_dict[name])

    logger.info("Reading input params...")

    # Open the file for reading
    fin = h5.File(fname, "r")

    group = fin["InputParams"]

    props_dict = dict(list(group.attrs.items()))
    arr_to_value(props_dict)
    group.visititems(visitfunc)

    # Update some properties
    if h is not None:
        logger.info("Scaling params to h = %.3f" % h)
        props_dict["BoxSize"] = group.attrs["BoxSize"][0] / h
        props_dict["PartMass"] = group.attrs["PartMass"][0] / h

    # Add extra props
    if not raw:
        props_dict["Volume"] = props_dict["BoxSize"] ** 3.0 * props_dict["VolumeFactor"]

        info = read_git_info(fname)
        props_dict.update({"model_git_ref": info[0], "model_git_diff": info[1]})

    fin.close()

    return props_dict


def read_units(fname):
    """ Read in the units and hubble conversion information from a Meraxes hdf5
    output file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file.

    Returns
    -------
    units : dict
        A dict containing all units (Hubble conversions are stored with key
        `HubbleConversions`).
    """

    def arr_to_value(d):
        for k, v in d.items():
            if type(v) is np.ndarray and v.size == 1:
                d[k] = v[0]

    def visitunits(name, obj):
        if isinstance(obj, h5.Group):
            units_dict[name] = dict(list(obj.attrs.items()))
            arr_to_value(units_dict[name])

    def visitconv(name, obj):
        if isinstance(obj, h5.Group):
            hubble_conv_dict[name] = dict(list(obj.attrs.items()))
            arr_to_value(hubble_conv_dict[name])

    def sanitize_dict_strings(d):
        regex = re.compile("(\D\.\S*)|(__.*__)|(__)")
        for k, v in d.items():
            if type(v) is dict:
                sanitize_dict_strings(v)
            else:
                v = v.decode("ascii")
                d[k] = re.sub(regex, "", v)

    logger.info("Reading units...")

    # Open the file for reading
    fin = h5.File(fname, "r")

    # Read the units
    for name in ["Units", "HubbleConversions"]:
        group = fin[name]
        if name == "Units":
            units_dict = dict(list(group.attrs.items()))
            arr_to_value(units_dict)
            group.visititems(visitunits)
        if name == "HubbleConversions":
            hubble_conv_dict = dict(list(group.attrs.items()))
            arr_to_value(hubble_conv_dict)
            group.visititems(visitconv)

    # Sanitize the hubble conversions
    sanitize_dict_strings(hubble_conv_dict)

    # Put the hubble conversions information inside the units dict for ease
    units_dict["HubbleConversions"] = hubble_conv_dict

    fin.close()

    return units_dict


def read_git_info(fname):
    """Read the git diff and ref saved in the master file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file.

    Returns
    -------
    ref : str
        git ref of the model

    diff : str
        git diff of the model
    """

    with h5.File(fname, "r") as fin:
        gitdiff = fin["gitdiff"][()]
        gitref = fin["gitdiff"].attrs["gitref"].copy()

    return gitref, gitdiff


def read_snaplist(fname, h=None):

    """ Read in the list of available snapshots from the Meraxes hdf5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file.

    h : float
        Hubble constant (/100) to scale the galaxy properties to.  If
        `None` then no scaling is made unless `set_little_h` was previously
        called.  (default = None)

    Returns
    -------
    snaps : array
        snapshots

    redshifts : array
        redshifts

    lt_times : array
        light travel times (Myr)
    """

    if (h is None) and (__meraxes_h is not None):
        h = __meraxes_h

    zlist = []
    snaplist = []
    lt_times = []

    with h5.File(fname, "r") as fin:
        for snap in list(fin.keys()):
            try:
                zlist.append(fin[snap].attrs["Redshift"][0])
                snaplist.append(int(snap[-3:]))
                lt_times.append(fin[snap].attrs["LTTime"][0])
            except KeyError:
                pass

    lt_times = np.array(lt_times, dtype=float)
    if h is not None:
        logger.info("Scaling lt_times to h = %.3f" % h)
        lt_times /= h

    return np.array(snaplist, dtype=int), np.array(zlist, dtype=float), lt_times


def check_for_redshift(fname, redshift, tol=0.1):
    """Check a Meraxes output file for the presence of a particular
    redshift.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    redshift : float
        Redshift value

    tol : float
        +- tolerance on redshift value present.  An error will be thrown of
        no redshift within this tollerance is found.

    Returns
    -------
    snapshot : int
        Closest snapshot

    redshift : float
        Closest corresponding redshift
    """

    snaps, z, lt_times = read_snaplist(fname)
    zs = z - redshift

    w = np.argmin(np.abs(zs))

    if np.abs(zs[w]) > tol:
        raise KeyError("No redshifts within tolerance found.")

    return int(snaps[w]), z[w]


def check_for_global_xH(fname, xH, tol=0.1):
    """Check a Meraxes output file for the presence of a particular
    global neutral fraction.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    xH : float
        Neutral fraction value

    tol : float
        +- tolerance on neutral fraction value present.  An error will be
        thrown of no redshift within this tollerance is found.

    Returns
    -------
    snapshot : int
        Closest snapshot

    redshift : float
        Closest corresponding redshift

    xH : float
        Closest corresponding redshift
    """

    snaps, z, lt_times = read_snaplist(fname)
    xH_list = read_global_xH(fname, snaps)
    xH_list[np.isnan(xH_list)] = -999
    delta_xH = xH - xH_list

    w = np.argmin(np.abs(delta_xH))

    if np.abs(delta_xH[w]) > tol:
        raise KeyError("No neutral fractions found within tolerance.")

    return int(snaps[w]), z[w], xH_list[w]


def grab_redshift(fname, snapshot):

    """ Quickly grab the redshift value of a single snapshot from a Meraxes
    HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot for which the redshift is to grabbed

    Returns
    -------
    redshift : float
        Corresponding redshift value
    """

    with h5.File(fname, "r") as fin:
        if snapshot < 0:
            present_snaps = np.asarray(list(fin.keys()))
            selection = np.array([(p.find("Snap") == 0) for p in present_snaps])
            present_snaps = [int(p[4:]) for p in present_snaps[selection]]
            snapshot = sorted(present_snaps)[snapshot]
        redshift = fin["Snap{:03d}".format(snapshot)].attrs["Redshift"][0]

    return redshift


def grab_unsampled_snapshot(fname, snapshot):

    """ Quickly grab the unsampled snapshot value of a single snapshot from a
    Meraxes HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot for which the unsampled value is to be grabbed

    Returns
    -------
    redshift : float
        Corresponding unsampled snapshot value
    """

    with h5.File(fname, "r") as fin:
        redshift = fin["Snap{:03d}".format(snapshot)].attrs["UnsampledSnapshot"][0]

    return redshift


def read_firstprogenitor_indices(fname, snapshot, pandas=False):

    """ Read the FirstProgenitor indices from the Meraxes HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot from which the progenitors dataset is to be read from.

    pandas : bool
        Return a pandas series instead of a numpy array.  (default = False)


    Returns
    -------
    fp_ind : array or series
        FirstProgenitor indices
    """

    if pandas:
        _check_pandas()

    with h5.File(fname, "r") as fin:

        # number of cores used for this run
        n_cores = fin.attrs["NCores"][0]

        # group in the master file for this snapshot
        snap_group = fin["Snap{:03d}".format(snapshot)]

        # group for the previous snapshot
        prev_snap_group = fin["Snap{:03d}".format(snapshot - 1)]

        # number of galaxies in this snapshot
        n_gals = snap_group.attrs["NGalaxies"][0]

        # malloc the fp_ind array and an array that will hold offsets for
        # each core
        fp_ind = np.zeros(n_gals, "i4")
        prev_core_counter = np.zeros(n_cores, "i4")

        # calculate the offsets for each core
        prev_core_counter[0] = 0
        for i_core in range(n_cores - 1):
            prev_core_counter[i_core + 1] = prev_snap_group["Core{:d}/Galaxies".format(i_core)].size
        prev_core_counter = np.cumsum(prev_core_counter)

        # loop through and read in the FirstProgenitorIndices for each core. Be
        # sure to update the value to reflect that we are making one big array
        # from the output of all cores. Also be sure *not* to update fp indices
        # that = -1.  This has special meaning!
        counter = 0
        for i_core in range(n_cores):
            ds = snap_group["Core{:d}/FirstProgenitorIndices".format(i_core)]
            core_nvals = ds.size
            if core_nvals > 0:
                dest_sel = np.s_[counter : core_nvals + counter]
                ds.read_direct(fp_ind, dest_sel=dest_sel)
                counter += core_nvals
                fp_ind[dest_sel][fp_ind[dest_sel] > -1] += prev_core_counter[i_core]

    if pandas:
        fp_ind = pd.Series(fp_ind)

    return fp_ind


def read_nextprogenitor_indices(fname, snapshot, pandas=False):

    """ Read the NextProgenitor indices from the Meraxes HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot from which the progenitors dataset is to be read from.

    pandas : bool
        Return a pandas series instead of a numpy array.  (default = False)

    Returns
    -------
    np_ind : array
        NextProgenitor indices
    """

    if pandas:
        _check_pandas()

    with h5.File(fname, "r") as fin:

        # number of cores used for this run
        n_cores = fin.attrs["NCores"][0]

        # group in the master file for this snapshot
        snap_group = fin["Snap{:03d}".format(snapshot)]

        # number of galaxies in this snapshot
        n_gals = snap_group.attrs["NGalaxies"][0]

        # malloc the np_ind array
        np_ind = np.zeros(n_gals, "i4")

        # loop through and read in the NextProgenitorIndices for each core. Be
        # sure to update the value to reflect that we are making one big array
        # from the output of all cores. Also be sure *not* to update np indices
        # that = -1.  This has special meaning!
        counter = 0
        for i_core in range(n_cores):
            ds = snap_group["Core{:d}/NextProgenitorIndices".format(i_core)]
            core_nvals = ds.size
            if core_nvals > 0:
                dest_sel = np.s_[counter : core_nvals + counter]
                ds.read_direct(np_ind, dest_sel=dest_sel)
                np_ind[dest_sel][np_ind[dest_sel] > -1] += counter
                counter += core_nvals

    if pandas:
        np_ind = pd.Series(np_ind)

    return np_ind


def read_descendant_indices(fname, snapshot, pandas=False):

    """ Read the Descendant indices from the Meraxes HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot from which the descendant dataset is to be read from.

    pandas : bool
        Return a pandas series instead of a numpy array.  (default = False)

    Returns
    -------
    desc_ind : array
        NextProgenitor indices
    """

    if pandas:
        _check_pandas()

    with h5.File(fname, "r") as fin:

        # number of cores used for this run
        n_cores = fin.attrs["NCores"][0]

        # group in the master file for this snapshot
        snap_group = fin["Snap{:03d}".format(snapshot)]

        # group for the next snapshot
        next_snap_group = fin["Snap{:03d}".format(snapshot + 1)]

        # number of galaxies in this snapshot
        n_gals = snap_group.attrs["NGalaxies"][0]

        # malloc the desc_ind array and an array that will hold offsets for
        # each core
        desc_ind = np.zeros(n_gals, "i4")
        prev_core_counter = np.zeros(n_cores, "i4")

        # calculate the offsets for each core
        prev_core_counter[0] = 0
        for i_core in range(n_cores - 1):
            prev_core_counter[i_core + 1] = next_snap_group["Core{:d}/Galaxies".format(i_core)].size
        prev_core_counter = np.cumsum(prev_core_counter)

        # loop through and read in the DescendantIndices for each core. Be sure
        # to update the value to reflect that we are making one big array from
        # the output of all cores. Also be sure *not* to update desc indices
        # that = -1.  This has special meaning!
        counter = 0
        for i_core in range(n_cores):
            ds = snap_group["Core{:d}/DescendantIndices".format(i_core)]
            core_nvals = ds.size
            if core_nvals > 0:
                dest_sel = np.s_[counter : core_nvals + counter]
                ds.read_direct(desc_ind, dest_sel=dest_sel)
                counter += core_nvals
                desc_ind[dest_sel][desc_ind[dest_sel] > -1] += prev_core_counter[i_core]

    if pandas:
        desc_ind = pd.Series(desc_ind)

    return desc_ind


def read_grid(fname, snapshot, name, h=None, h_scaling={}):

    """ Read a grid from the Meraxes HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot from which the grid is to be read from.

    name : str
        Name of the requested grid

    h : float
        Hubble constant (/100) to scale the galaxy properties to.  If
        `None` then no scaling is made unless `set_little_h` was previously
        called.  (default = None)

    h_scaling : dict
        Dictionary of grid names (keys) and associated Hubble
        constant scalings (values) as lambda functions. e.g.
        | h_scaling = {"MassLikeGrid" : lambda x, h: x/h,}

    Returns
    -------
        ndarray
            The requested grid
    """

    if (h is None) and (__meraxes_h is not None):
        h = __meraxes_h

    with h5.File(fname, "r") as fin:
        try:
            grid_dim = fin["InputParams"].attrs["ReionGridDim"][0]
        except KeyError:
            grid_dim = fin["InputParams"].attrs["TOCF_HII_dim"][0]
        ds_name = "Snap{:03d}/Grids/{:s}".format(snapshot, name)
        try:
            grid = fin[ds_name][:]
        except KeyError:
            logger.error("No grid called %s found in file %s ." % (name, fname))

    # Apply any Hubble scalings
    if h is not None:
        h = float(h)
        units = read_units(fname)
        h_conv = units["HubbleConversions"]["Grids"]

        logger.info("Scaling grid to h = %.3f" % h)
        try:
            conversion = h_conv[name]
        except KeyError:
            logger.warn("Unknown scaling for grid %s - assuming no " "scaling with Hubble const!" % name)
            conversion = "None"

        if conversion.lower() != "none":
            try:
                grid = eval(conversion, dict(v=grid, h=h, log10=np.log10, __builtins__={}))
            except:
                logger.error("Failed to parse conversion string `%s` for unit" " %s" % (conversion, name))

    grid.shape = [grid_dim,] * 3

    return grid


def list_grids(fname, snapshot):

    """ List the available grids from a Meraxes HDF5 output file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot for which the grids are to be listed.

    Returns
    -------
    grids : list
        A list of the available grids
    """

    with h5.File(fname, "r") as fin:
        group_name = "Snap{:03d}/Grids".format(snapshot)
        try:
            grids = list(fin[group_name].keys())
        except KeyError:
            logger.error("No grids found for snapshot %d in file %s ." % (snapshot, fname))

    return grids


def read_ps(fname, snapshot):

    """ Read 21cm power spectrum from the Meraxes HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot from which the power spectrum is to be read from.

    Returns
    -------
    kval : array
        k value (Mpc^-1)

    ps : array
        power value (should be dimensionless but actually might be power
        density i.e. with units [Mpc^-3])

    pserr : array
        error
    """

    with h5.File(fname, "r") as fin:
        ds_name = "Snap{:03d}/PowerSpectrum".format(snapshot)
        try:
            ps_nbins = fin[ds_name].attrs["nbins"][0]
            ps = fin[ds_name][:]
        except KeyError:
            logger.error("No data called found in file %s ." % (fname))

    ps.shape = [ps_nbins, 3]

    return ps[:, 0], ps[:, 1], ps[:, 2]


def read_size_dist(fname, snapshot):

    """ Read region size distribution from the Meraxes HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int
        Snapshot from which the region size distribution is to be read
        from.

    Returns
    -------
    Rval : array
        R value

    RdpdR : array
        RdpdR value
    """

    with h5.File(fname, "r") as fin:
        ds_name = "Snap{:03d}/RegionSizeDist".format(snapshot)
        try:
            R_nbins = fin[ds_name].attrs["nbins"][0]
            RdpdR = fin[ds_name][:]
        except KeyError:
            logger.error("No RegionSizeDist found in file %s ." % (fname))

    RdpdR.shape = [R_nbins, 2]

    return RdpdR[:, 0], RdpdR[:, 1]


def read_global_xH(fname, snapshot, weight="volume"):

    """ Read global xH from the Meraxes HDF5 file.

    Parameters
    ----------
    fname : str
        Full path to input hdf5 master file

    snapshot : int or list
        Snapshot(s) from which the global xH is to be read
        from.

    weight : str
        'volume' -> volume weighted
        'mass' -> mass weighted

    Returns
    -------
    global_xH : float or ndarray
        Global xH value(s)
    """

    if not hasattr(snapshot, "__len__"):
        snapshot = [
            snapshot,
        ]

    if weight == "volume":
        prop = "volume_weighted_global_xH"
    elif weight == "mass":
        prop = "mass_weighted_global_xH"
    else:
        raise ValueError("Unrecognized weighting scheme: %s" % weight)

    snapshot = np.array(snapshot)
    global_xH = np.zeros(snapshot.size)

    with h5.File(fname, "r") as fin:
        for ii, snap in enumerate(snapshot):
            ds_name = "Snap{:03d}/Grids/xH".format(snap)
            try:
                global_xH[ii] = fin[ds_name].attrs[prop][0]
            except KeyError:
                if weight == "volume":
                    # This case deals with old style Meraxes file outputs
                    try:
                        global_xH[ii] = fin[ds_name].attrs["global_xH"][0]
                    except KeyError:
                        pass
                    else:
                        continue

                global_xH[ii] = np.nan
                logger.warning("No global_xH found for snapshot %d in file %s" % (snap, fname))

    if snapshot.size == 1:
        return global_xH[0]
    else:
        return global_xH
