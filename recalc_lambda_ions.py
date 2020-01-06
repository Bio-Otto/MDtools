################################################################
# Kevin Shen, 2019                                             #
# kevinshen@ucsb.edu                                           #
#                                                              #
# General purpose openMM simulation script.                    #
# Allows for (verlet, langevin); barostats; LJPME              #
# Adds Uext potential
# simulation protocol:                                         #
#   1) equilibrate                                             #
#   2) production run                                          #
#Doesn't write no-water config, unlike simDCD.py               #
################################################################


# System stuff
from sys import stdout
import time
import os,sys,logging
logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel('INFO')
sh = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(fmt='%(asctime)s - %(message)s', datefmt="%H:%M:%S")
sh.setFormatter(formatter)
import argparse
import alchemifyIons
from collections import namedtuple, defaultdict, OrderedDict
import numpy as np

# OpenMM Imports
import simtk.openmm as mm
import simtk.openmm.app as app
import simtk.unit as unit

# ParmEd & MDTraj Imports
import parmed.openmm
from parmed import gromacs
#gromacs.GROMACS_TOPDIR = "/home/kshen/SDS"
from parmed.openmm.reporters import NetCDFReporter
from mdtraj.reporters import DCDReporter
from parmed import unit as u
import parmed as pmd
import mdtraj

# Custom Tools
import mdparse




def add_barostat(system,args):
    if args.pressure <= 0.0:
        logger.info("This is a constant volume (NVT) run")
    else:
        logger.info("This is a constant pressure (NPT) run at %.2f bar pressure" % args.pressure)
        logger.info("Adding Monte Carlo barostat with volume adjustment interval %i" % args.nbarostat)
        logger.info("Anisotropic box scaling is %s" % ("ON" if args.anisotropic else "OFF"))
        if args.anisotropic:
            logger.info("Only the Z-axis will be adjusted")
            barostat = mm.MonteCarloAnisotropicBarostat(Vec3(args.pressure*u.bar, args.pressure*u.bar, args.pressure*u.bar), args.temperature*u.kelvin, False, False, True, args.nbarostat)
        else:
            barostat = mm.MonteCarloBarostat(args.pressure * u.bar, args.temperature * u.kelvin, args.nbarostat)
        system.addForce(barostat)
    '''
    else:
        args.deactivate("pressure", msg="System is nonperiodic")
        #raise Exception('Pressure was specified but the topology contains no periodic box! Exiting...')
    '''


def set_thermo(system,args):
    '''
    Takes care of thermostat if needed
    '''
    if args.temperature <= 0.0:
        logger.info("This is a constant energy, constant volume (NVE) run.")
        integrator = mm.VerletIntegrator(2.0*u.femtoseconds)
    else:
        logger.info("This is a constant temperature run at %.2f K" % args.temperature)
        logger.info("The stochastic thermostat collision frequency is %.2f ps^-1" % args.collision_rate)
        if args.integrator == "langevin":
            logger.info("Creating a Langevin integrator with %.2f fs timestep." % args.timestep)
            integrator = mm.LangevinIntegrator(args.temperature * u.kelvin, 
                                                args.collision_rate / u.picoseconds, 
                                                args.timestep * u.femtosecond)
        elif args.integrator == "verlet":
            integrator = mm.VerletIntegrator(2.0*u.femtoseconds)
            thermostat = mm.AndersenThermostat(args.temperature * u.kelvin, args.collision_rate / u.picosecond)
            system.addForce(thermostat)
        else:
            logger.warning("Unknown integrator, will crash now")
        add_barostat(system,args)
    return integrator


def main(paramfile='params.in', overrides={}, quiktest=False, deviceid=None, progressreport=True, soluteRes=[0],lambdaLJ=1.0,lambdaQ=1.0, ewldTol=1e-7, trajfile="", outfile=""): #simtime=2.0, T=298.0, NPT=True, LJcut=10.0, tail=True, useLJPME=False, rigidH2O=True, device=0, quiktest=False):
    # === PARSE === #
    args = mdparse.SimulationOptions(paramfile, overrides)
   
    # paperwork
    assert trajfile, "Must provide a trajectory file to recalculate on"
    logger.info("Reading in trajectory from {}".format(trajfile))
    if not outfile:
        outfile = "lamLJ{}_lamQ{}_resid{}".format(lambdaLJ,lambdaQ,soluteRes)
        logger.info("Default output: {}".format(outfile))
    args.force_active('minimize',val=False,msg="Recalculating, don't minimize")


    # Files
    gromacs.GROMACS_TOPDIR = args.topdir
    top_file        = args.topfile
    box_file        = args.grofile
    defines         = {}
    cont            = args.cont


    args.force_active('chkxml',val='chk_{:02n}.xml'.format(cont),msg='first one')
    args.force_active('chkpdb',val='chk_{:02n}.pdb'.format(cont),msg='first one')
    if cont > 0:
        args.force_active('incoord',val='chk_{:02n}.xml'.format(cont-1),msg='continuing')
        args.force_active('outpdb',val='output_{:02n}.pdb'.format(cont),msg='continuing')
        args.force_active('outnetcdf',val='output_{:02n}.nc'.format(cont),msg='continuing')
        args.force_active('logfile',val='thermo.log_{:02n}'.format(cont),msg='continuing')
        args.force_active('outdcd',val='output_{:02n}.dcd'.format(cont),msg='continuing')


    logger.info("Recalculating energies for free energy, force ewald tolerance to be tighter for reproducibility")
    ewald_error_tolerance=ewldTol
    args.force_active('ewald_error_tolerance',val=ewald_error_tolerance,msg='free energy calculation needs tighter Ewald tolearnce')
    args.force_active('cuda_precision',val='double',msg='free energy calculation needs higher precision')

    incoord         = args.incoord
    out_pdb         = args.outpdb
    out_netcdf      = args.outnetcdf
    out_dcd         = args.outdcd
    molecTopology   = 'topology.pdb'
    out_nowater     = 'output_nowater.nc'
    out_nowater_dcd = 'output_nowater.dcd'
    logfile         = args.logfile
    checkpointxml   = args.chkxml
    checkpointpdb   = args.chkpdb
    checkpointchk   = 'chk_{:02n}.chk'.format(cont)

    # Parameters
    #Temp            = args.temperature        #K
    #Pressure = 1      #bar
    #barostatfreq    = 25 #time steps
    #fric            = args.collision_rate     #1/ps

    dt              = args.timestep 	      #fs
    if args.use_fs_interval:
        reportfreq = int(args.report_interval/dt)
        netcdffreq = int(args.netcdf_report_interval/dt) #5e4
        dcdfreq    = int(args.dcd_report_interval/dt)
        pdbfreq    = int(args.pdb_report_interval/dt)
        checkfreq  = int(args.checkpoint_interval/dt)
        #simtime    = int( simtime ) #nanoseconds; make sure division is whole... no remainders...
        blocksteps = int(args.block_interval/dt)   #1e6, steps per block of simulation 
        nblocks    = args.nblocks #aiming for 1 block is 1ns
    else:
        reportfreq = args.report_interval
        netcdffreq = args.netcdf_report_interval
        dcdfreq    = args.dcd_report_interval
        pdbfreq    = args.pdb_report_interval
        checkfreq  = args.checkpoint_interval
        blocksteps = args.block_interval
        nblocks    = args.nblocks 

    if quiktest==True:
        reportfreq = 1
        blocksteps = 10
        nblocks = 2

    # === Start Making System === # 
    start = time.time()
    top = gromacs.GromacsTopologyFile(top_file, defines=defines)
    gro = gromacs.GromacsGroFile.parse(box_file)
    top.box = gro.box
    logger.info("Took {}s to create topology".format(time.time()-start))
    print(top)

    constr = {None: None, "None":None,"HBonds":app.HBonds,"HAngles":app.HAngles,"AllBonds":app.AllBonds}[args.constraints]   
    start = time.time()
    system = top.createSystem(nonbondedMethod=app.PME, ewaldErrorTolerance = args.ewald_error_tolerance,
                        nonbondedCutoff=args.nonbonded_cutoff*u.nanometers,
                        rigidWater = args.rigid_water, constraints = constr)
    logger.info("Took {}s to create system".format(time.time()-start))
                          
 
    nbm = {"NoCutoff":mm.NonbondedForce.NoCutoff, "CutoffNonPeriodic":mm.NonbondedForce.CutoffNonPeriodic,
                "Ewald":mm.NonbondedForce.Ewald, "PME":mm.NonbondedForce.PME, "LJPME":mm.NonbondedForce.LJPME}[args.nonbonded_method]

    ftmp = [f for ii, f in enumerate(system.getForces()) if isinstance(f,mm.NonbondedForce)]
    fnb = ftmp[0]
    fnb.setNonbondedMethod(nbm)
    logger.info("Nonbonded method ({},{})".format(args.nonbonded_method, fnb.getNonbondedMethod()) )
    if (not args.dispersion_correction) or (args.nonbonded_method=="LJPME"):
        logger.info("Turning off tail correction...")
        fnb.setUseDispersionCorrection(False)
        logger.info("Check dispersion flag: {}".format(fnb.getUseDispersionCorrection()) )

    # --- execute custom forcefield code ---
    """
    if customff:
        logger.info("Using customff: [{}]".format(customff))
        with open(customff,'r') as f:
            ffcode = f.read()
        exec(ffcode,globals(),locals()) #python 3, need to pass in globals to allow exec to modify them (i.e. the system object)
        #print(sys.path)
        #sys.path.insert(1,'.')
        #exec("import {}".format(".".join(customff.split(".")[:-1])))
    else:
        logger.info("--- No custom ff code provided ---")

    fExts=[f for f in system.getForces() if isinstance(f,mm.CustomExternalForce)]
    logger.info("External forces added: {}".format(fExts))
    """
    soluteIndices = []
    soluteResidues = soluteRes #list of residues to alchemify. modified s.t. soluteRes is already a list
    #parmed gromacs topology
    for ir,res in enumerate(top.residues):
        if ir in soluteResidues:
            for atom in res.atoms:
                soluteIndices.append(atom.idx)
    logger.info("Solute residue: {}".format([top.residues[ir].atoms for ir in soluteResidues]))
    logger.info("Solute Indices: {}".format(soluteIndices))
    #if using openmm topology. unfortunately don't know how to convert from parmed to openmm#:
    #topology = parmed.openmm.load_topology(top.topology)
    #print(type(topology))
    #for ir,res in topology.residues():
    #    if ir in soluteResidues:
    #        for atom in res.atoms:
    #            soluteIndices.append(atom.index)

    alch = alchemifyIons.alchemist(system,lambdaLJ,lambdaQ)
    alch.setupSolute(soluteIndices)
    logger.info(system.getForces())
    


    # === Integrator, Barostat, Additional Constraints === #
    integrator = set_thermo(system,args)

    if not hasattr(args,'constraints') or (str(args.constraints) == "None" and args.rigid_water == False):
        args.deactivate('constraint_tolerance',"There are no constraints in this system")
    else:
        logger.info("Setting constraint tolerance to %.3e" % args.constraint_tolerance)
        integrator.setConstraintTolerance(args.constraint_tolerance)


    # === Make Platform === #
    logger.info("Setting Platform to %s" % str(args.platform))
    try:
        platform = mm.Platform.getPlatformByName(args.platform)
    except:
        logger.info("Warning: %s platform not found, going to Reference platform \x1b[91m(slow)\x1b[0m" % args.platform)
        args.force_active('platform',"Reference","The %s platform was not found." % args.platform)
        platform = mm.Platform.getPlatformByName("Reference")

    if deviceid is not None or deviceid>=0:
        args.force_active('device',deviceid,msg="Using cmdline-input deviceid")
    if 'device' in args.ActiveOptions and (platform.getName()=="OpenCL" or platform.getName()=="CUDA"):
        device = str(args.device)
        # The device may be set using an environment variable or the input file.
        #if 'CUDA_DEVICE' in os.environ.keys(): #os.environ.has_key('CUDA_DEVICE'):
        #    device = os.environ.get('CUDA_DEVICE',str(args.device))
        #elif 'CUDA_DEVICE_INDEX' in os.environ.keys(): #os.environ.has_key('CUDA_DEVICE_INDEX'):
        #    device = os.environ.get('CUDA_DEVICE_INDEX',str(args.device))
        #else:
        #    device = str(args.device)
        if device != None:
            logger.info("Setting Device to %s" % str(device))
            #platform.setPropertyDefaultValue("CudaDevice", device)
            if platform.getName()=="CUDA":
                platform.setPropertyDefaultValue("CudaDeviceIndex", device)
            elif platform.getName()=="OpenCL":
                logger.info("set OpenCL device to {}".format(device))
                platform.setPropertyDefaultValue("OpenCLDeviceIndex", device)
        else:
            logger.info("Using the default (fastest) device")
    else:
        logger.info("Using the default (fastest) device, or not using CUDA nor OpenCL")

    if "Precision" in platform.getPropertyNames() and (platform.getName()=="OpenCL" or platform.getName()=="CUDA"):
        platform.setPropertyDefaultValue("Precision", args.cuda_precision)
    else:
        logger.info("Not setting precision")
        args.deactivate("cuda_precision",msg="Platform does not support setting cuda_precision.")

    # === Create Simulation === #
    logger.info("Creating the Simulation object")
    start = time.time()
    # Get the number of forces and set each force to a different force group number.
    nfrc = system.getNumForces()
    if args.integrator != 'mtsvvvr':
        for i in range(nfrc):
            system.getForce(i).setForceGroup(i)
    '''
    for i in range(nfrc):
        # Set vdW switching function manually.
        f = system.getForce(i)
        if f.__class__.__name__ == 'NonbondedForce':
            if 'vdw_switch' in args.ActiveOptions and args.vdw_switch:
                f.setUseSwitchingFunction(True)
                f.setSwitchingDistance(args.switch_distance)
    '''

    #create simulation object
    if args.platform != None:
        simulation = app.Simulation(top.topology, system, integrator, platform)
    else:
        simulation = app.Simulation(top.topology, system, integrator)
    topomm = mdtraj.Topology.from_openmm(simulation.topology)
    logger.info("System topology: {}".format(topomm))


    #print platform we're using
    mdparse.printcool_dictionary({i:simulation.context.getPlatform().getPropertyValue(simulation.context,i) for i in simulation.context.getPlatform().getPropertyNames()},title="Platform %s has properties:" % simulation.context.getPlatform().getName())


    logger.info("--== PME parameters ==--")
    ftmp = [f for ii, f in enumerate(simulation.system.getForces()) if isinstance(f,mm.NonbondedForce)]
    fnb = ftmp[0]   
    if fnb.getNonbondedMethod() == 4: #check for PME
        PMEparam = fnb.getPMEParametersInContext(simulation.context)
        logger.info(fnb.getPMEParametersInContext(simulation.context))
    if fnb.getNonbondedMethod() == 5: #check for LJPME
        PMEparam = fnb.getLJPMEParametersInContext(simulation.context)
        logger.info(fnb.getLJPMEParametersInContext(simulation.context))
    #nmeshx = int(PMEparam[1]*1.5)
    #nmeshy = int(PMEparam[2]*1.5)
    #nmeshz = int(PMEparam[3]*1.5)
    #fnb.setPMEParameters(PMEparam[0],nmeshx,nmeshy,nmeshz)
    #logger.info(fnb.getPMEParametersInContext(simulation.context))


    # Print out some more information about the system
    logger.info("--== System Information ==--")
    logger.info("Number of particles   : %i" % simulation.context.getSystem().getNumParticles())
    logger.info("Number of constraints : %i" % simulation.context.getSystem().getNumConstraints())
    for f in simulation.context.getSystem().getForces():
        if f.__class__.__name__ == 'NonbondedForce':
            method_names = ["NoCutoff", "CutoffNonPeriodic", "CutoffPeriodic", "Ewald", "PME", "LJPME"]
            logger.info("Nonbonded method      : %s" % method_names[f.getNonbondedMethod()])
            logger.info("Number of particles   : %i" % f.getNumParticles())
            logger.info("Number of exceptions  : %i" % f.getNumExceptions())
            if f.getNonbondedMethod() > 0:
                logger.info("Nonbonded cutoff      : %.3f nm" % (f.getCutoffDistance() / u.nanometer))
                if f.getNonbondedMethod() >= 3:
                    logger.info("Ewald error tolerance : %.3e" % (f.getEwaldErrorTolerance()))
                logger.info("LJ switching function : %i" % f.getUseSwitchingFunction())
                if f.getUseSwitchingFunction():
                    logger.info("LJ switching distance : %.3f nm" % (f.getSwitchingDistance() / u.nanometer))

    # Print the sample input file here.
    for line in args.record():
        print(line)

    logger.info("Took {}s to make and setup simulation object".format(time.time()-start))

    #============================#
    #| Initialize & Eq/Warm-Up  |#
    #============================#

    p = simulation.context.getPlatform()
    if p.getName()=="CUDA" or p.getName()=="OpenCL":
        logger.info("simulation platform: {}".format(p.getName()) )
        logger.info(p.getPropertyNames())
        logger.info(p.getPropertyValue(simulation.context,'DeviceName'))
        logger.info("Device Index: {}".format(p.getPropertyValue(simulation.context,'DeviceIndex')))
        logger.info("Precision: {}".format(p.getPropertyValue(simulation.context,'Precision')))

    if os.path.exists(args.restart_filename) and args.read_restart:
        logger.info("Restarting simulation from the restart file.")
        logger.info("Currently is filler")
    else:
        # Set initial positions.
        if incoord.split(".")[-1]=="pdb":
            pdb = app.PDBFile(incoord) #pmd.load_file(incoord)
            simulation.context.setPositions(pdb.positions)
            logger.info('Set positions from pdb, {}'.format(incoord))
            molecTopology = incoord
        elif incoord.split(".")[-1]=="xyz":
            traj = mdtraj.load(incoord, top = mdtraj.Topology.from_openmm(simulation.topology))
            simulation.context.setPositions( traj.openmm_positions(0) )
        elif incoord.split(".")[-1]=="xml":
            simulation.loadState(incoord)
            logger.info('Set positions from xml, {}'.format(incoord))
            logger.info("Need to make sure to set Global lambda parameters properly. The charges in the standard Nonbonded Force should've already been set by alchemify.")
            logger.info( 'parameters after loading xml: (lambdaLJ, {}), (lambdaQ, {})'.format(simulation.context.getParameter('lambdaLJ'), simulation.context.getParameter('lambdaQ')))
            simulation.context.setParameter('lambdaLJ',lambdaLJ)
            simulation.context.setParameter('lambdaQ',lambdaQ)
            logger.info( 'parameters after setting properly: (lambdaLJ, {}), (lambdaQ, {})'.format(simulation.context.getParameter('lambdaLJ'), simulation.context.getParameter('lambdaQ')))
        else:
            logger.info("Error, can't handle input coordinate filetype")
        
        if args.constraint_tolerance > 0.0:    
            simulation.context.applyConstraints(args.constraint_tolerance) #applies constraints in current frame.
        logger.info("Initial potential energy is: {}".format(simulation.context.getState(getEnergy=True).getPotentialEnergy()) )

        if args.integrator != 'mtsvvvr':
            eda = mdparse.EnergyDecomposition(simulation)
            eda_kcal = OrderedDict([(i, "%10.4f" % (j/4.184)) for i, j in eda.items()])
            mdparse.printcool_dictionary(eda_kcal, title="Energy Decomposition (kcal/mol)")


    #============================#
    #|   Recalculate Energies   |#
    #============================#
    if incoord.split(".")[-1]=="pdb":
        traj = mdtraj.load(trajfile, top=args.incoord)
    elif incoord.split(".")[-1]=="xml": #workaround, since if using continue flag > 0, I force input to be from previous xml file
        traj = mdtraj.load(trajfile, top=args.chkpdb)

    PE = np.zeros(traj.n_frames) 
    for it,t in enumerate(traj):
        if np.mod(it,100) == 0:
            logger.info("...Frame {}".format(it))
        box = t.unitcell_vectors[0]
        simulation.context.setPeriodicBoxVectors(box[0], box[1], box[2])
        simulation.context.setPositions(t.xyz[0])
        state = simulation.context.getState(getEnergy=True)
        PE[it] = state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        
    with open(outfile,'w') as f:
        f.write("#frame\tPE(kJ/mol), ewald error tolerance: {}\n".format(args.ewald_error_tolerance))
        for ie,energy in enumerate(PE):
            f.write("{}\t{}\n".format(ie,energy))



#END main()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Simulation Properties')
    parser.add_argument("paramfile", default='params.in', type=str, help="param.in file")
    parser.add_argument("trajfile", type=str, help="trajectory file. Topology uses the incoord in the paramfile.")
    parser.add_argument("--outfile", default="", type=str, help="place to write re-calculated energies")
    parser.add_argument("--deviceid", default=-1, type=int, help="GPU device id")
    parser.add_argument("--progressreport", default=True, type=bool, help="Whether or not to print progress report. Incurs small overhead")
    parser.add_argument("-soluteRes", action='append', type=int, help="Solute residue index to alchemify")
    parser.add_argument("-lambdaLJ", type=float, default=1.0, help="lamdaLJ coupling, default 1.0")
    parser.add_argument("-lambdaQ", type=float, default=1.0, help="lamdaQ coupling, default 1.0")
    parser.add_argument('-ewldTol', default=1e-7, type=float, help="ewld tolerance. default is 1e-7")
    
    #parser.add_argument("--customff", default="", type=str, help="Custom force field python script to run after generating system")
    #parser.add_argument("simtime", type=float, help="simulation runtime (ns)")
    #parser.add_argument("Temp", type=float, help="system Temperature")
    #parser.add_argument("--NPT", action="store_true", help="NPT flag")
    #parser.add_argument("LJcut", type=float, help="LJ cutoff (Angstroms)")
    cmdln_args = parser.parse_args()


    #================================#
    #    The command line parser     #
    #================================#
    '''
    # Taken from MSMBulder - it allows for easy addition of arguments and allows "-h" for help.
    def add_argument(group, *args, **kwargs):
        if 'default' in kwargs:
            d = 'Default: {d}'.format(d=kwargs['default'])
            if 'help' in kwargs:
                kwargs['help'] += ' {d}'.format(d=d)
            else:
                kwargs['help'] = d
        group.add_argument(*args, **kwargs)

    print
    print " #===========================================#"
    print " #|    OpenMM general purpose simulation    |#"
    print " #| (Hosted @ github.com/leeping/OpenMM-MD) |#"
    print " #|  Use the -h argument for detailed help  |#"
    print " #===========================================#"
    print

    parser = argparse.ArgumentParser()
    add_argument(parser, 'pdb', nargs=1, metavar='input.pdb', help='Specify one PDB or AMBER inpcrd file \x1b[1;91m(Required)\x1b[0m', type=str)
    add_argument(parser, 'xml', nargs='+', metavar='forcefield.xml', help='Specify multiple force field XML files, one System XML file, or one AMBER prmtop file \x1b[1;91m(Required)\x1b[0m', type=str)
    add_argument(parser, '-I', '--inputfile', help='Specify an input file with options in simple two-column format.  This script will autogenerate one for you', default=None, type=str)
    cmdline = parser.parse_args()
    pdbfnm = cmdline.pdb[0]
    xmlfnm = cmdline.xml
    args = SimulationOptions(cmdline.inputfile, pdbfnm)
    '''

    # === RUN === #
    main(cmdln_args.paramfile, {}, deviceid=cmdln_args.deviceid, progressreport=cmdln_args.progressreport, soluteRes=cmdln_args.soluteRes, lambdaLJ=cmdln_args.lambdaLJ, lambdaQ=cmdln_args.lambdaQ, ewldTol=cmdln_args.ewldTol, trajfile=cmdln_args.trajfile, outfile=cmdln_args.outfile)

#End __name__

