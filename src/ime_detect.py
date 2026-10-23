# -*- coding: utf-8 -*-
"""输入法检测：读当前活动输入法的 CLSID，判断搜狗 / 微软拼音。

搜狗 TIP CLSID   = {E7EA138E-69F8-11D7-A6EA-00065B844310}
微软拼音 TIP CLSID = {81D4E9C9-1D3B-41BC-9E6C-4B40BF79E35E}
"""
import comtypes
from comtypes import GUID, COMMETHOD, HRESULT, IUnknown
from ctypes import c_ushort, c_ulong, c_int, c_void_p, c_wchar_p, POINTER, byref

CLSID_TF_InputProcessorProfiles = GUID("{33C53A50-F456-4884-B049-85FD643ECFED}")
IID_ITfInputProcessorProfileMgr = GUID("{71C6E74C-0F28-11D8-A82A-00065B84435C}")
GUID_TFCAT_TIP_KEYBOARD = GUID("{34745C63-B2F0-4784-8B67-5E12C8701A31}")

CLSID_SOGOU = "{E7EA138E-69F8-11D7-A6EA-00065B844310}"
CLSID_MSPY = "{81D4E9C9-1D3B-41BC-9E6C-4B40BF79E35E}"

REFGUID = POINTER(GUID)


class TF_INPUTPROCESSORPROFILE(comtypes.Structure):
    _fields_ = [
        ('dwProfileType', c_ulong), ('langid', c_ushort), ('clsid', GUID),
        ('guidProfile', GUID), ('catid', GUID), ('hklSubstitute', c_void_p),
        ('dwCaps', c_ulong), ('hkl', c_void_p), ('dwFlags', c_ulong),
    ]


class ITfInputProcessorProfileMgr(IUnknown):
    _iid_ = IID_ITfInputProcessorProfileMgr
    _methods_ = [
        COMMETHOD([], HRESULT, 'ActivateProfile',
                  (['in'], c_ulong, 'dwProfileType'), (['in'], c_ushort, 'langid'),
                  (['in'], REFGUID, 'clsid'), (['in'], REFGUID, 'guidProfile'),
                  (['in'], c_void_p, 'hkl'), (['in'], c_ulong, 'dwFlags')),
        COMMETHOD([], HRESULT, 'DeactivateProfile',
                  (['in'], c_ulong, 'dwProfileType'), (['in'], c_ushort, 'langid'),
                  (['in'], REFGUID, 'clsid'), (['in'], REFGUID, 'guidProfile'),
                  (['in'], c_void_p, 'hkl'), (['in'], c_ulong, 'dwFlags')),
        COMMETHOD([], HRESULT, 'GetProfile',
                  (['in'], c_ulong, 'dwProfileType'), (['in'], c_ushort, 'langid'),
                  (['in'], REFGUID, 'clsid'), (['in'], REFGUID, 'guidProfile'),
                  (['in'], c_void_p, 'hkl'),
                  (['out'], POINTER(TF_INPUTPROCESSORPROFILE), 'pProfile')),
        COMMETHOD([], HRESULT, 'EnumProfiles',
                  (['in'], c_ushort, 'langid'), (['out'], POINTER(c_void_p), 'ppEnum')),
        COMMETHOD([], HRESULT, 'ReleaseInputProcessor',
                  (['in'], REFGUID, 'rclsid'), (['in'], c_ulong, 'dwFlags')),
        COMMETHOD([], HRESULT, 'RegisterProfile',
                  (['in'], REFGUID, 'rclsid'), (['in'], c_ushort, 'langid'),
                  (['in'], REFGUID, 'guidProfile'), (['in'], c_wchar_p, 'pchDesc'),
                  (['in'], c_ulong, 'cchDesc'), (['in'], c_wchar_p, 'pchIconFile'),
                  (['in'], c_ulong, 'cchFile'), (['in'], c_ulong, 'uIconIndex'),
                  (['in'], c_void_p, 'hklsubstitute'), (['in'], c_ulong, 'dwPreferredLayout'),
                  (['in'], c_int, 'bEnabledByDefault'), (['in'], c_ulong, 'dwFlags')),
        COMMETHOD([], HRESULT, 'UnregisterProfile',
                  (['in'], REFGUID, 'rclsid'), (['in'], c_ushort, 'langid'),
                  (['in'], REFGUID, 'guidProfile'), (['in'], c_ulong, 'dwFlags')),
        COMMETHOD([], HRESULT, 'GetActiveProfile',
                  (['in'], REFGUID, 'catid'),
                  (['out'], POINTER(TF_INPUTPROCESSORPROFILE), 'pProfile')),
    ]


def get_active_clsid():
    """返回当前活动键盘输入法 CLSID 字符串；失败返回 None。"""
    try:
        unk = comtypes.CoCreateInstance(CLSID_TF_InputProcessorProfiles,
                                        interface=IUnknown, clsctx=comtypes.CLSCTX_INPROC_SERVER)
        mgr = unk.QueryInterface(ITfInputProcessorProfileMgr)
        prof = mgr.GetActiveProfile(byref(GUID_TFCAT_TIP_KEYBOARD))
        return str(prof.clsid)
    except Exception:
        return None


def ime_kind(clsid=None):
    """返回 'sogou' / 'mspy' / 'other' / None。"""
    clsid = clsid or get_active_clsid()
    if not clsid:
        return None
    if clsid.upper().startswith(CLSID_SOGOU):
        return "sogou"
    if clsid.upper().startswith(CLSID_MSPY):
        return "mspy"
    return "other"


if __name__ == "__main__":
    c = get_active_clsid()
    print("active clsid =", c, "->", ime_kind(c))
