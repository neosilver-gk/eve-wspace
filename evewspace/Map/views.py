from Map.models import *
from Map import utils
from API import utils as handler
from POS import tasks as pos_tasks
from POS.models import POS, Corporation
from core.models import Type
from django.core.exceptions import PermissionDenied, ObjectDoesNotExist
from django.http import Http404, HttpResponseRedirect, HttpResponse
from django.template.response import TemplateResponse
from django.core.urlresolvers import reverse
from django.template import RequestContext
from django.template.loader import render_to_string
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render, get_object_or_404
from django.conf import settings
from datetime import datetime, timedelta, time
import pytz
import json
import eveapi

# Decorator to check map permissions. Takes request and mapID
# Permissions are 0 = None, 1 = View, 2 = Change
# When used without a permission=x specification, requires Change access

def require_map_permission(permission=2):
    def _dec(view_func):
        def _view(request, mapID, *args, **kwargs):
            map = get_object_or_404(Map, pk=mapID)
            if map.get_permission(request.user) < permission:
                raise PermissionDenied
            else:
                return view_func(request, mapID, *args, **kwargs)
        _view.__name__ = view_func.__name__
        _view.__doc__ = view_func.__doc__
        _view.__dict__ = view_func.__dict__
        return _view
    return _dec

@login_required
@require_map_permission(permission=1)
def get_map(request, mapID):
    """Get the map and determine if we have permissions to see it.
    If we do, then return a TemplateResponse for the map. If map does not
    exist, return 404. If we don't have permission, return PermissionDenied.
    """
    map = get_object_or_404(Map, pk=mapID)
    context = {'map': map, 'access': map.get_permission(request.user),
            'systemsJSON': map.as_json(request.user)}
    return TemplateResponse(request, 'map.html', context)


@login_required
@require_map_permission(permission=1)
def map_checkin(request, mapID):
    # Initialize json return dict
    jsonvalues = {}
    profile = request.user.get_profile()
    currentmap = get_object_or_404(Map, pk=mapID)

    # Out AJAX requests should post a JSON datetime called loadtime
    # back that we use to get recent logs.
    if  'loadtime' not in request.POST:
        return HttpResponse(json.dumps({error: "No loadtime"}),mimetype="application/json")
    timestring = request.POST['loadtime']

    loadtime = datetime.strptime(timestring, "%Y-%m-%d %H:%M:%S.%f")
    loadtime = loadtime.replace(tzinfo=pytz.utc)

    if request.is_igb_trusted:
        dialogHtml = checkin_igb_trusted(request, currentmap)
        if dialogHtml is not None:
            jsonvalues.update({'dialogHTML': dialogHtml})

    newlogquery = MapLog.objects.filter(timestamp__gt=loadtime, visible=True,
            map=currentmap)
    loglist = []

    for log in newlogquery:
        loglist.append("<strong>User:</strong> %s <strong>Action:</strong> %s" % (
            log.user.username, log.action))

    logstring = render_to_string('log_div.html', {'logs': loglist})
    jsonvalues.update({'logs': logstring})

    return HttpResponse(json.dumps(jsonvalues), mimetype="application/json")

@login_required
@require_map_permission(permission=1)
def map_refresh(request, mapID):
    """
    Returns an HttpResponse with the updated systemJSON for an asynchronous
    map refresh.
    """
    if not request.is_ajax():
        raise PermissionDenied
    map = get_object_or_404(Map, pk=mapID)
    result = [datetime.strftime(datetime.now(pytz.utc), "%Y-%m-%d %H:%M:%S.%f"),
            utils.MapJSONGenerator(map, request.user).get_systems_json()]
    return HttpResponse(json.dumps(result))

def checkin_igb_trusted(request, map):
    """
    Runs the specific code for the case that the request came from an igb that
    trusts us, returns None if no further action is required, returns a string
    containing the html for a system add dialog if we detect that a new system
    needs to be added
    """
    profile = request.user.get_profile()
    currentsystem = System.objects.get(name=request.eve_systemname)
    oldsystem = None
    result = None

    if profile.currentsystem:
        oldsystem = profile.currentsystem

    #Conditions for the system to be automagically added to the map. The case
    #of oldsystem == None is handled by a condition on "sys in map" (None cannot
    #be in any map), the case oldsystem == currentsystem is handled by the
    #condition that if two systems are equal one cannot be in and the other not
    #in the same map (i.e 'oldsystem in map and currentsystem not in map' will be
    #False).
    if (
      oldsystem in map
      and currentsystem not in map
      #Stop it from adding everyone's paths through k-space to the map
      and not (oldsystem.is_kspace() and currentsystem.is_kspace())
      and profile.lastactive > datetime.now(pytz.utc) - timedelta(minutes=5)
      ):
        context = { 'oldsystem' : map.systems.filter(system=oldsystem).all()[0],
                    'newsystem' : currentsystem,
                    'wormholes'  : utils.get_possible_wh_types(oldsystem, currentsystem),
                  }
        result = render_to_string('igb_system_add_dialog.html', context,
                                  context_instance=RequestContext(request))

    currentsystem.add_active_pilot(request.user, request.eve_charname,
            request.eve_shipname, request.eve_shiptypename)
    return result

def get_system_context(msID):
    mapsys = get_object_or_404(MapSystem, pk=msID)
    currentmap = mapsys.map

    #if mapsys represents a k-space system get the relevent KSystem object
    if mapsys.system.is_kspace():
        system = mapsys.system.ksystem
    #otherwise get the relevant WSystem
    else:
        system = mapsys.system.wsystem

    scanthreshold = datetime.now(pytz.utc) - timedelta(hours=3)
    interestthreshold = datetime.now(pytz.utc) - timedelta(minutes=settings.MAP_INTEREST_TIME)

    scanwarning = system.lastscanned < scanthreshold
    interest = mapsys.interesttime and mapsys.interesttime > interestthreshold

    return { 'system' : system, 'mapsys' : mapsys,
             'scanwarning' : scanwarning, 'isinterest' : interest }


@login_required
@require_map_permission(permission=2)
def add_system(request, mapID):
    """
    AJAX view to add a system to a map. Requires POST containing:
       topMsID: MapSystem ID of the parent MapSystem
       bottomSystem: Name of the new system
       topType: WormholeType name of the parent side
       bottomType: WormholeType name of the new side
       timeStatus: Womrhole time status integer value
       massStatus: Wormhole mass status integer value
       topBubbled: 1 if Parent side bubbled
       bottomBubbled: 1 if new side bubbled
       friendlyName: Friendly name for the new MapSystem
    """
    if not request.is_ajax():
       raise PermissionDenied
    try:
        # Prepare data
        map = Map.objects.get(pk=mapID)
        topMS = MapSystem.objects.get(pk=request.POST.get('topMsID'))
        bottomSys = System.objects.get(name=request.POST.get('bottomSystem'))
        topType = WormholeType.objects.get(name=request.POST.get('topType'))
        bottomType = WormholeType.objects.get(name=request.POST.get('bottomType'))
        timeStatus = int(request.POST.get('timeStatus'))
        massStatus = int(request.POST.get('massStatus'))
        topBubbled = "1" == request.POST.get('topBubbled')
        bottomBubbled = "1" == request.POST.get('bottomBubbled')
        # Add System
        bottomMS = map.add_system(request.user, bottomSys,
                request.POST.get('friendlyName'), topMS)
        # Add Wormhole
        bottomMS.connect_to(topMS, topType, bottomType, topBubbled,
                bottomBubbled, timeStatus, massStatus)

        return HttpResponse('[]')
    except ObjectDoesNotExist:
        return HttpResponse(status=400)


@login_required
@require_map_permission(permission=2)
def remove_system(request, mapID, msID):
    """
    Removes the supplied MapSystem from a map.
    """
    system = get_object_or_404(MapSystem, pk=msID)
    system.remove_system(request.user)
    return HttpResponse('[]')


@login_required
@require_map_permission(permission=1)
def system_details(request, mapID, msID):
    """
    Returns a html div representing details of the System given by msID in
    map mapID
    """
    if not request.is_ajax():
        raise PermissionDenied

    return render(request, 'system_details.html', get_system_context(msID))

@login_required
@require_map_permission(permission=1)
def system_menu(request, mapID, msID):
    """
    Returns the html for system menu
    """
    if not request.is_ajax():
        raise PermissionDenied

    return render(request, 'system_menu.html', get_system_context(msID))

@login_required
@require_map_permission(permission=1)
def system_tooltip(request, mapID, msID):
    """
    Returns a system tooltip for msID in mapID
    """
    if not request.is_ajax():
        raise PermissionDenied

    return render(request, 'system_tooltip.html', get_system_context(msID))


@login_required
@require_map_permission(permission=1)
def wormhole_tooltip(request, mapID, whID):
    """Takes a POST request from AJAX with a Wormhole ID and renders the
    wormhole tooltip for that ID to response.

    """
    if request.is_ajax():
        wh = get_object_or_404(Wormhole, pk=whID)
        return HttpResponse(render_to_string("wormhole_tooltip.html",
            {'wh': wh}, context_instance=RequestContext(request)))
    else:
        raise PermissionDenied


@login_required()
@require_map_permission(permission=2)
def mark_scanned(request, mapID, msID):
    """Takes a POST request from AJAX with a system ID and marks that system
    as scanned.

    """
    if request.is_ajax():
        mapsys = get_object_or_404(MapSystem, pk=msID)
        mapsys.system.lastscanned = datetime.now(pytz.utc)
        mapsys.system.save()
        return HttpResponse('[]')
    else:
        raise PermissionDenied


@login_required()
def manual_location(request, mapID, msID):
    """Takes a POST request form AJAX with a System ID and marks the user as
    being active in that system.

    """
    if request.is_ajax():
        mapsystem = get_object_or_404(MapSystem, pk=msID)
        mapsystem.system.add_active_pilot(request.user, "OOG Browser",
                "Unknown", "Uknown")
        return HttpResponse("[]")
    else:
        raise PermissionDenied


@login_required()
@require_map_permission(permission=2)
def set_interest(request, mapID, msID):
    """Takes a POST request from AJAX with an action and marks that system
    as having either utcnow or None as interesttime. The action can be either
    "set" or "remove".

    """
    if request.is_ajax():
        action = request.POST.get("action","none")
        if action == "none":
            raise Http404
        system = get_object_or_404(MapSystem, pk=msID)
        if action == "set":
            system.interesttime = datetime.now(pytz.utc)
            system.save()
            return HttpResponse('[]')
        if action == "remove":
            system.interesttime = None
            system.save()
            return HttpResponse('[]')
        return HttpResponse(staus=418)
    else:
        raise PermissionDenied

@login_required()
@require_map_permission(permission=2)
def add_signature(request, mapID, msID):
    """This function processes the Add Signature form. GET gets the form
    and POST submits it and returns either a blank JSON list or a form with errors.
    All requests should be AJAX.

    """
    if not request.is_ajax():
        raise PermissionDenied
    mapsystem = get_object_or_404(MapSystem, pk=msID)

    if request.method == 'POST':
        form = SignatureForm(request.POST)
        if form.is_valid():
            newSig = form.save(commit=False)
            newSig.system = mapsystem.system
            newSig.sigid = newSig.sigid.upper()
            newSig.updated = True
            newSig.save()
            newForm = SignatureForm()
            mapsystem.map.add_log(request.user, "Added signature %s to %s (%s)."
                    % (newSig.sigid, mapsystem.system.name, mapsystem.friendlyname))
            return TemplateResponse(request, "add_sig_form.html",
                    {'form': newForm, 'system': mapsystem})
        else:
            return TemplateResponse(request, "add_sig_form.html",
                    {'form': form, 'system': mapsystem})
    else:
        form = SignatureForm()
    return TemplateResponse(request, "add_sig_form.html",
            {'form': form, 'system': mapsystem})


@login_required
@require_map_permission(permission=2)
def edit_signature(request, mapID, msID, sigID):
    """
    GET gets a pre-filled edit signature form. POST updates the signature with the new information and returns a blank add form.
    """
    if not request.is_ajax():
        raise PermissionDenied
    signature = get_object_or_404(Signature, pk=sigID)
    mapsys = get_object_or_404(MapSystem, pk=msID)

    if request.method == 'POST':
        form = SignatureForm(request.POST)
        if form.is_valid():
            signature.sigid = request.POST['sigid'].upper()
            signature.updated = True
            signature.info = request.POST['info']
            signature.sigtype = SignatureType.objects.get(pk=request.POST['sigtype'])
            signature.save()
            mapsys.map.add_log(request.user, "Updated signature %s in %s (%s)" %
                    (signature.sigid, mapsys.system.name, mapsys.friendlyname))
            return TemplateResponse(request, "add_sig_form.html",
                    {'form': SignatureForm(), 'system': mapsys})
        else:
            return TemplateResponse(request, "edit_sig_form.html",
                    {'form': form, 'system': mapsys, 'sig': signature})
    else:
        return TemplateResponse(request, "edit_sig_form.html",
                {'form': SignatureForm(instance=signature), 'system': mapsys, 'sig': signature})

@login_required()
@require_map_permission(permission=1)
def get_signature_list(request, mapID, msID):
    """
    Determines the proper escalationThreshold time and renders
    system_signatures.html
    """
    if not request.is_ajax():
        raise PermissionDenied
    system = get_object_or_404(MapSystem, pk=msID)
    return TemplateResponse(request, "system_signatures.html",
        {'system': system})


@login_required
@require_map_permission(permission=2)
def mark_signature_cleared(request, mapID, msID, sigID):
    """
    Marks a signature as having its NPCs cleared.
    """
    if not request.is_ajax():
        raise PermissionDenied
    sig = get_object_or_404(Signature, pk=sigID)
    sig.clear_rats()
    return HttpResponse('[]')


@login_required
@require_map_permission(permission=2)
def escalate_site(request, mapID, msID, sigID):
    """
    Marks a site as having been escalated.
    """
    if not request.is_ajax():
        raise PermissionDenied
    sig = get_object_or_404(Signature, pk=sigID)
    sig.escalate()
    return HttpResponse('[]')


@login_required
@require_map_permission(permission=2)
def activate_signature(request, mapID, msID, sigID):
    """
    Marks a site activated.
    """
    if not request.is_ajax():
        raise PermissionDenied
    sig = get_object_or_404(Signature, pk=sigID)
    sig.activate()
    return HttpResponse('[]')


@login_required
@require_map_permission(permission=2)
def delete_signature(request, mapID, msID, sigID):
    """
    Deletes a signature.
    """
    if not request.is_ajax():
        raise PermissionDenied
    mapsys = get_object_or_404(MapSystem, pk=msID)
    sig = get_object_or_404(Signature, pk=sigID)
    sig.delete()
    mapsys.map.add_log(request.user, "Deleted signature %s in %s (%s)."
            % (sig.sigid, mapsys.system.name, mapsys.friendlyname))
    return HttpResponse('[]')


@login_required
@require_map_permission(permission=2)
def manual_add_system(request, mapID, msID):
    """
    A GET request gets a blank add system form with the provided mapSystem
    as top system. The form is then POSTed to the add_system view.
    """
    topMS = get_object_or_404(MapSystem, pk=msID)
    systems = System.objects.all()
    wormholes = WormholeType.objects.all()
    return render(request, 'add_system_box.html', {'topMs': topMS,
        'sysList': systems, 'whList': wormholes})


@login_required
@require_map_permission(permission=2)
def edit_system(request, mapID, msID):
    """
    A GET request gets the edit system dialog pre-filled with current information.
    A POST request saves the posted data as the new information.
        POST values are friendlyName, info, and occupied.
    """
    if not request.is_ajax():
        raise PermissionDenied
    mapSystem = get_object_or_404(MapSystem, pk=msID)
    if request.method == 'GET':
        occupied = mapSystem.system.occupied.replace("<br />", "\n")
        info = mapSystem.system.info.replace("<br />", "\n")
        return TemplateResponse(request, 'edit_system.html', {'mapsys': mapSystem,
            'occupied': occupied, 'info': info})
    if request.method == 'POST':
        mapSystem.friendlyname = request.POST.get('friendlyName', '')
        mapSystem.system.info = request.POST.get('info', '')
        mapSystem.system.occupied = request.POST.get('occupied', '')
        mapSystem.system.save()
        mapSystem.save()
        mapSystem.map.add_log(request.user, "Edited System: %s (%s)"
                % (mapSystem.system.name, mapSystem.friendlyname))
        return HttpResponse('[]')
    raise PermissionDenied


@login_required
@require_map_permission(permission=2)
def edit_wormhole(request, mapID, whID):
    """
    A GET request gets the edit wormhole dialog pre-filled with current info.
    A POST request saves the posted data as the new info.
        POST values are topType, bottomType, massStatus, timeStatus, topBubbled,
        and bottomBubbled.
    """
    if not request.is_ajax():
        raise PermissionDenied
    wormhole = get_object_or_404(Wormhole, pk=whID)
    if request.method == 'GET':
        return TemplateResponse(request, 'edit_wormhole.html', {'wormhole': wormhole})
    if request.method == 'POST':
        wormhole.mass_status = int(request.POST.get('massStatus',0))
        wormhole.time_status = int(request.POST.get('timeStatus',0))
        wormhole.top_type = get_object_or_404(WormholeType,
                name=request.POST.get('topType', 'K162'))
        wormhole.bottom_type = get_object_or_404(WormholeType,
                name=request.POST.get('bottomType', 'K162'))
        wormhole.top_bubbled = request.POST.get('topBubbled', '1') == '1'
        wormhole.bottom_bubbled = request.POST.get('bottomBubbled', '1') == '1'
        wormhole.save()
        wormhole.map.add_log(request.user, "Updated the wormhole between %s(%s) and %s(%s)."
                % (wormhole.top.system.name, wormhole.top.friendlyname,
                    wormhole.bottom.system.name, wormhole.bottom.friendlyname))
        return HttpResponse('[]')

    raise PermissiondDenied


@permission_required('Map.add_map')
def create_map(request):
    """
    This function creates a map and then redirects to the new map.
    """
    if request.method == 'POST':
        form = MapForm(request.POST)
        if form.is_valid():
            newMap = form.save()
            newMap.add_log(request.user, "Created the %s map." % (newMap.name))
            newMap.add_system(request.user, newMap.root, "Root", None)
            return HttpResponseRedirect(reverse('Map.views.get_map',
                kwargs={'mapID': newMap.pk }))
    else:
        form = MapForm
        return TemplateResponse(request, 'new_map.html', { 'form': form, })


@require_map_permission(permission=1)
def destination_list(request, mapID, msID):
    """
    Returns the destinations of interest list for K-space systems and
    a blank response for w-space systems. The results are cached in the template
    as long as possible since they will never change for a System.
    """
    #if not request.is_ajax():
    #    raise PermissionDenied
    destinations = Destination.objects.all()
    mapsys = get_object_or_404(MapSystem, pk=msID)
    try:
        system = KSystem.objects.get(pk=mapsys.system.pk)
    except:
        return HttpResponse('')
    return render(request, 'system_destinations.html', {'system': system,
        'destinations': destinations})


def site_spawns(request, mapID, msID, sigID):
    """
    Returns the spawns for a given signature and system.
    """
    sig = get_object_or_404(Signature, pk=sigID)
    spawns = SiteSpawn.objects.filter(sigtype=sig.sigtype).all()
    if spawns[0].sysclass != 0:
        spawns = SiteSpawn.objects.filter(sigtype=sig.sigtype,
                sysclass=sig.system.sysclass).all()
    return render(request, 'site_spawns.html', {'spawns': spawns})
