# coding: utf-8

'''
module: fmlopack.io.nro45m
author: Akio Taniguchi
affill: Institute of Astronomy, the University of Tokyo
mailto: taniguchi_at_ioa.s.u-tokyo.ac.jp
'''

version  = '1.0'

# ==============================================================================
# ==============================================================================
try:
    import builtins
    import tkinter
except ImportError:
    import __builtin__ as builtins
    import Tkinter as tkinter

import os
import re
import sys
import shutil
import tempfile
import tkFileDialog
from datetime import datetime
from decimal  import Decimal
from subprocess import Popen, PIPE

import numpy  as np
import pandas as pd
from astropy.io import fits as pf
import scipy.interpolate as ip

import fmlopack.fm.fmscan as fms


# ==============================================================================
# ==============================================================================
def load(obstable, sam45log, fmlolog, antlog, array_ids='all', **kwargs):
    hdulist = Nro45mFmlo()
    hdulist._obstable(obstable)
    hdulist._sam45dict()
    hdulist._fmlolog(fmlolog)
    hdulist._antlog(antlog)
    hdulist._sam45log(sam45log, array_ids)
    hdulist.info()

    return hdulist

def open(fitsname=None, mode='readonly', memmap=None, save_backup=False, **kwargs):
    if fitsname == '' or fitsname is None:
        root = tkinter.Tk()
        root.withdraw()
        fitsname = tkFileDialog.askopenfilename()
        root.destroy()
        if fitsname == '': return

    hdulist = Nro45mFmlo.fromfile(fitsname, mode, memmap, save_backup, **kwargs)
    hdulist._sam45dict()
    hdulist.info()

    return hdulist


# ==============================================================================
# ==============================================================================
class Nro45mFmlo(pf.HDUList):
    # --------------------------------------------------------------------------
    def __init__(self, hdus=[], file=None):
        pf.HDUList.__init__(self, hdus, file)

    def version(self):
        return self['PRIMARY'].header['VERSION']

    # --------------------------------------------------------------------------
    def fmscan(self, array_id, binning=1, cutedge=0, time_offset=0):
        '''
        Return a FmScan of the selected array ID (e.g. A5)
        '''
        header   = self[array_id].header
        scan_wid = header['NAXIS1']/binning
        scan_len = header['NAXIS2']
        chan_wid = header['BANDWID']/scan_wid

        # scan and tsys
        scan_raw = self[array_id].data
        tsys_raw = np.asarray(header['TSYS'].split(','), 'f8')
        scan_bin = scan_raw.reshape((scan_len, scan_wid, binning)).mean(axis=2)
        tsys_bin = tsys_raw.reshape((scan_wid, binning)).mean(axis=1)
        if cutedge == 0:
            scan = scan_bin
            tsys = tsys_bin
        else:
            scan = scan_bin[:,cutedge:-cutedge]
            tsys = tsys_bin[cutedge:-cutedge]

        # freq range
        fmlolog    = self['FMLOLOG'].data[time_offset+3:time_offset+scan_len+3]
        chan_fm    = fmlolog.FREQFM / chan_wid
        freq_min   = header['RESTFREQ'] - 0.5*(scan_wid-2*cutedge-1)*chan_wid
        freq_max   = header['RESTFREQ'] + 0.5*(scan_wid-2*cutedge-1)*chan_wid
        freq_min  += chan_fm * chan_wid
        freq_max  += chan_fm * chan_wid
        freq_range = np.vstack((freq_min, freq_max)).T

        # radec
        antlog = self['ANTLOG'].data
        time_min = pd.to_datetime(pd.Series(fmlolog.TIME)).min()
        time_max = pd.to_datetime(pd.Series(fmlolog.TIME)).max()
        time_idx = pd.to_datetime(pd.Series(antlog.TIME))
        radec_df = pd.DataFrame(antlog.RADEC, time_idx)[time_min:time_max]
        #radec    = np.asarray(radec_df.resample('100L').interpolate())
        radec    = np.asarray(radec_df.resample('100L').max()) # test ...

        # other components
        date_time = fmlolog.TIME
        interval  = np.tile(header['CDELT2'], scan_len)
        fmstatus  = 'modulated'

        # make fmrecord
        alist  = [chan_fm, freq_range, interval, date_time, radec]
        names  = ['CHANFM', 'FREQRANGE', 'INTERVAL', 'DATETIME', 'RADEC']
        dtypes = ['i8', 'f8', 'f8', 'a26', 'f8']
        shapes = [1, 2, 1, 1, 2]
        fmrecord = np.rec.fromarrays(alist, zip(names, dtypes, shapes))

        return fms.FmScan(scan, tsys, fmrecord, fmstatus)

    # --------------------------------------------------------------------------
    def _obstable(self, obstable):
        '''
        Load a obstable and append a PrimaryHDU to HDUList
        '''
        hdu = pf.PrimaryHDU()
        hdu.header['ORGFILE'] = obstable.split('/')[-1], 'Original file'
        hdu.header['VERSION'] = version, 'Version of fmlopack'

        idx = 0
        for line in builtins.open(obstable, 'r'):
            if re.search('Initialize', line): break

            if re.search('^SET SAM45', line): key_type = 'SAM'
            elif re.search('^SET ANT', line): key_type = 'ANT'
            elif re.search('^SET MRG', line): key_type = 'MRG'
            elif re.search('^SET RXT', line): key_type = 'RXT'
            elif re.search('^SET IFATT', line): key_type = 'ATT'
            elif re.search('^SET GRPTRK', line): key_type = 'GRP'
            elif re.search('^SET SYNTHE_H', line): key_type = 'SYH'
            elif re.search('^SET SYNTHE_E', line): key_type = 'SYE'
            else: continue

            key  = '{}-{:0>3}'.format(key_type, idx)
            item = '{}={}'.format(line.split()[2], line.split()[3].strip("(')"))
            hdu.header[key] = item
            idx += 1

        self.append(hdu)

    # --------------------------------------------------------------------------
    def _sam45dict(self):
        '''
        Append a dict containing various parameters of SAM45
        '''
        hdr = self['PRIMARY'].header
        dic = dict()

        for key_hdr in hdr:
            if not(re.search('^SAM', key_hdr)): continue

            key      = hdr[key_hdr].split('=')[0]
            item     = hdr[key_hdr].split('=')[1].split(',')
            dtype    = self._sam45dict_config(key, 'dtype')
            shape    = self._sam45dict_config(key, 'shape')
            item     = np.array(item, dtype).reshape(shape)
            dic[key] = item.tolist()[0] if len(item)==1 else item

        self.sam45dict = dic

    # --------------------------------------------------------------------------
    def _sam45log(self, sam45log, array_ids='all', tamb=293.0, sldump='sldump'):
        '''
        Load a SAM45 logging and append ImageHDU(s) to HDUList.
        '''
        # obstable info from obstdict
        sam = self.sam45dict
        array_use = sam['ARRAY'] == 1
        array_max = sam['ARRAY'].sum()
        scan_wid  = np.max(sam['CH_RANGE'])
        scan_len  = int(sam['INTEG_TIME'] / sam['IPTIM'])
        idx_to_id = lambda idx: 'A{}'.format(idx+1)
        id_to_idx = lambda aid: int(aid.strip('A'))-1

        if array_ids == 'all':
            #array_idx = range(array_max)
            array_idx = np.where(array_use)[0]
        else:
            array_ids = sorted(array_ids)
            array_idx = map(id_to_idx, array_ids)

        # dump sam45log using sldump (external code)
        print('dumping:  {}'.format(sam45log))
        dump_dir  = tempfile.mkdtemp(dir=os.environ['HOME'])
        dump_file = dump_dir+'/dump.txt'
        proc = Popen([sldump, sam45log, dump_file, '1', '4096'], stderr=PIPE)
        proc.communicate()

        # load dumped sam45log
        print('loading:  {} (temporary file)'.format(dump_file))

        zero = np.empty(scan_wid*array_max, 'f8')
        r    = np.empty(scan_wid*array_max, 'f8')
        sky  = np.empty(scan_wid*array_max, 'f8')
        on   = np.empty((scan_len, scan_wid*array_max), 'f8')

        try:
            f = builtins.open(dump_file, 'r')
            for (idx, line) in enumerate(f):
                i, j = idx // array_max, idx % array_max
                time_slice = np.asarray(line.split()[3:], 'f8')

                # zero, r, sky, on
                if   i == 0: zero[scan_wid*j: scan_wid*(j+1)] = time_slice
                elif i == 1: r[scan_wid*j: scan_wid*(j+1)]    = time_slice
                elif i == 2: sky[scan_wid*j: scan_wid*(j+1)]  = time_slice
                elif 3 <= i < scan_len+3:
                    on[i-3, scan_wid*j: scan_wid*(j+1)] = time_slice

        finally:
            print('removing: {}'.format(dump_file))
            f.close()
            shutil.rmtree(dump_dir)

        # calibration and tsys
        att  = np.repeat(sam['IFATT'][array_use], scan_wid)
        tsys = tamb / (10**(0.1*att) * ((r-zero)/(sky-zero)) - 1)
        #scan = tamb * (on-np.median(on,0)) / (10**(0.1*att)*(r-zero)-(sky-zero))
        #scan = on - zero
        scan = on

        print(array_idx)
        print(np.where(array_use)[0])

        # append ImageHDUs
        for j in range(array_max):
            idx = np.where(array_use)[0][j]
            #if not(j in array_idx): continue
            if not idx in array_idx: continue

            #if sam['SIDBD_TYP'][j] == 'USB':
            if sam['SIDBD_TYP'][idx] == 'USB':
                hdu_data = scan[:, scan_wid*j: scan_wid*(j+1)]
                hdu_tsys = tsys[scan_wid*j: scan_wid*(j+1)]
                hdu_tsys_str = str(list(hdu_tsys)).strip('[]')

            #elif sam['SIDBD_TYP'][j] == 'LSB':
            elif sam['SIDBD_TYP'][idx] == 'LSB':
                hdu_data = scan[:, scan_wid*j: scan_wid*(j+1)][:,::-1]
                hdu_tsys = tsys[scan_wid*j: scan_wid*(j+1)][::-1]
                hdu_tsys_str = str(list(hdu_tsys)).strip('[]')

            hdu = pf.ImageHDU()
            hdu.data = hdu_data
            #hdu.header['EXTNAME']  = idx_to_id(j), 'Name of HDU'
            hdu.header['EXTNAME']  = idx_to_id(idx), 'Name of HDU'
            hdu.header['ORGFILE']  = sam45log.split('/')[-1], 'Original file'
            hdu.header['OBJECT']   = sam['SRC_NAME']
            hdu.header['RA']       = sam['SRC_POS'][0], 'Right Ascention (deg)'
            hdu.header['DEC']      = sam['SRC_POS'][1], 'Declination (deg)'
            hdu.header['BANDWID']  = sam['OBS_BAND'][j], 'Band width (Hz)'
            hdu.header['RESTFREQ'] = sam['REST_FREQ'][j], 'Rest frequency (Hz)'
            hdu.header['SIDEBAND'] = sam['SIDBD_TYP'][j], 'USB or LSB'
            hdu.header['CTYPE1']   = 'Spectral'
            hdu.header['CUNIT1']   = 'ch'
            hdu.header['CDELT1']   = hdu.header['BANDWID'] / hdu.header['NAXIS1']
            hdu.header['CTYPE2']   = 'Time'
            hdu.header['CDELT2']   = sam['IPTIM']
            hdu.header['CUNIT2']   = '{} sec'.format(sam['IPTIM'])
            hdu.header['BSCALE']   = 1.0, 'PHYSICAL = PIXEL*BSCALE + BZERO'
            hdu.header['BZERO']    = 0.0
            hdu.header['BUNIT']    = 'K'
            hdu.header['BTYPE']    = 'Intensity'
            hdu.header['TAMB']     = tamb, 'Ambient temperature (K)'
            hdu.header['TSYS']     = hdu_tsys_str, 'System noise temperature (K)'

            self.append(hdu)

    # --------------------------------------------------------------------------
    def _fmlolog(self, fmlolog, skiprows=1):
        '''
        Load a fmlolog and append a BinTableHDU to HDUList.
        '''
        time    = []
        status  = []
        freq_fm = []
        freq_lo = []
        v_rad   = []

        # load fmlolog
        f = builtins.open(fmlolog, 'r')
        for (i, line) in enumerate(f):
            if i < skiprows: continue

            items = line.split()
            time_fmt_r = '%Y%m%d%H%M%S.%f'
            time_fmt_w = '%Y-%m-%dT%H:%M:%S.%f'
            time_str   = '{:.6f}'.format(Decimal(items[0]))
            time_dt    = datetime.strptime(time_str, time_fmt_r)

            time.append(time_dt.strftime(time_fmt_w))
            status.append(items[1])
            freq_fm.append(items[2])
            freq_lo.append(items[3])
            v_rad.append(items[4])

        f.close()

        # append BinTableHDU
        alist  = [time, freq_fm, freq_lo, v_rad]
        names  = ['TIME', 'FREQFM', 'FREQLO', 'VRAD']
        dtypes = ['a26', 'f8', 'f8', 'f8']

        hdu = pf.BinTableHDU()
        hdu.data = np.rec.fromarrays(alist, zip(names, dtypes))

        hdu.header['EXTNAME'] = 'FMLOLOG', 'Name of HDU'
        hdu.header['ORGFILE'] = fmlolog.split('/')[-1], 'Original file'
        hdu.header['TUNIT1']  = 'YYYY-MM-DDThh:mm:ss.ssssss'
        hdu.header['TUNIT2']  = 'Hz'
        hdu.header['TUNIT3']  = 'Hz'
        hdu.header['TUNIT4']  = 'km/s'

        self.append(hdu)

    # --------------------------------------------------------------------------
    def _antlog(self, antlog, skiprows=1):
        '''
        Load a antlog and append a BinTableHDU to HDUList
        '''
        time   = []
        radec  = []
        azel_1 = []
        azel_2 = []
        offset = []

        # load antlog
        f = builtins.open(antlog, 'r')
        for (i, line) in enumerate(f):
            if i < skiprows: continue

            items = line.split()
            time_fmt_r = '%y%m%d%H%M%S.%f'
            time_fmt_w = '%Y-%m-%dT%H:%M:%S.%f'
            time_str   = '{:.6f}'.format(Decimal(items[0]))
            time_dt    = datetime.strptime(time_str, time_fmt_r)

            time.append(time_dt.strftime(time_fmt_w))
            radec.append([items[1], items[2]])
            azel_1.append([items[3], items[4]])
            azel_2.append([items[5], items[6]])
            offset.append([items[7], items[8]])

        f.close()

        # append BinTableHDU
        alist  = [time, radec, azel_1, azel_2, offset]
        names  = ['TIME', 'RADEC', 'AZEL1', 'AZEL2', 'OFFSET']
        dtypes = ['a26', 'f8', 'f8', 'f8', 'f8']
        shapes = [1, 2, 2, 2, 2]

        hdu = pf.BinTableHDU()
        hdu.data = np.rec.fromarrays(alist, zip(names, dtypes, shapes))

        hdu.header['EXTNAME'] = 'ANTLOG', 'Name of HDU'
        hdu.header['ORGFILE'] = antlog.split('/')[-1], 'Original file'
        hdu.header['TUNIT1']  = 'YYYY-MM-DDThh:mm:ss.ssssss'
        hdu.header['TUNIT2']  = 'deg'
        hdu.header['TUNIT3']  = 'deg'
        hdu.header['TUNIT4']  = 'deg'
        hdu.header['TUNIT5']  = 'deg'

        self.append(hdu)

    # --------------------------------------------------------------------------
    def _sam45dict_config(self, key, prop):
        cfg = dict()
        cfg['INTEG_TIME']         = {'dtype': np.int,   'shape': None}
        cfg['CALB_INT']           = {'dtype': np.int,   'shape': None}
        cfg['IPTIM']              = {'dtype': np.float, 'shape': None}
        cfg['FREQ_INTVAL']        = {'dtype': np.int,   'shape': None}
        cfg['VELO']               = {'dtype': np.float, 'shape': None}
        cfg['MAP_POS']            = {'dtype': np.int,   'shape': None}
        cfg['FREQ_SW']            = {'dtype': np.int,   'shape': None}
        cfg['MULT_OFF']           = {'dtype': np.float, 'shape': None}
        cfg['MULT_NUM']           = {'dtype': np.int,   'shape': None}
        cfg['REF_NUM']            = {'dtype': np.int,   'shape': None}
        cfg['REST_FREQ']          = {'dtype': np.float, 'shape': None}
        cfg['OBS_FREQ']           = {'dtype': np.float, 'shape': None}
        cfg['FREQ_IF1']           = {'dtype': np.float, 'shape': None}
        cfg['OBS_BAND']           = {'dtype': np.float, 'shape': None}
        cfg['ARRAY']              = {'dtype': np.int,   'shape': None}
        cfg['IFATT']              = {'dtype': np.int,   'shape': None}
        cfg['FQDAT_F0']           = {'dtype': np.float, 'shape': None}
        cfg['FQDAT_FQ']           = {'dtype': np.float, 'shape': None}
        cfg['FQDAT_CH']           = {'dtype': np.int,   'shape': (32,2)}
        cfg['SRC_POS']            = {'dtype': np.float, 'shape': None}
        cfg['CH_BAND']            = {'dtype': np.int,   'shape': None}
        cfg['CH_RANGE']           = {'dtype': np.int,   'shape': (32,2)}
        cfg['QL_RMSLIMIT']        = {'dtype': np.float, 'shape': None}
        cfg['QL_POINTNUM']        = {'dtype': np.int,   'shape': None}
        cfg['BIN_NUM']            = {'dtype': np.int,   'shape': None}
        cfg['N_SPEC_WINDOW_SUB1'] = {'dtype': np.int,   'shape': None}
        cfg['START_CHAN_SUB1']    = {'dtype': np.int,   'shape': None}
        cfg['END_CHAN_SUB1']      = {'dtype': np.int,   'shape': None}
        cfg['CHAN_AVG_SUB1']      = {'dtype': np.int,   'shape': None}
        cfg['N_SPEC_WINDOW_SUB2'] = {'dtype': np.int,   'shape': None}
        cfg['START_CHAN_SUB2']    = {'dtype': np.int,   'shape': None}
        cfg['END_CHAN_SUB2']      = {'dtype': np.int,   'shape': None}
        cfg['CHAN_AVG_SUB2']      = {'dtype': np.int,   'shape': None}

        try: return cfg[key][prop]
        except: return None

# ==============================================================================
# ==============================================================================
class Nro45mPsw(object):
    def __init__(self, fitsname, sideband='USB', maskedge=1, useGHz=True):
        self.data   = pf.getdata(fitsname)
        self.header = pf.getheader(fitsname)
        self.sb     = sideband
        self.freq   = self.frequency(useGHz)
        self.spec   = self.spectrum(maskedge)
        self.rms    = None
        if self.sb == 'LSB':
            self.freq = self.freq[::-1]
            self.spec = self.spec[::-1]

    def frequency(self, useGHz=True):
        f     = 1e-9 if useGHz else 1.0
        f_sb  = -0.5 if self.sb == 'USB' else +0.5
        naxis = self.header['NAXIS2']
        crpix = self.header['CRPIX2']
        crval = float(self.data['CRVAL2'])
        cdelt = float(self.data['CDELT2'])
        return f * (crval + cdelt * (np.arange(naxis)-crpix+f_sb))

    def spectrum(self, maskedge):
        spec = self.data['DATA'][0]
        if maskedge: spec[:maskedge], spec[-maskedge:] = 0, 0
        return spec

    def interpolate(self, frequency):
        f = ip.interp1d(self.freq, self.spec, bounds_error=False)
        return f(frequency)


# ==============================================================================
# ==============================================================================
class Nro45mError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message
