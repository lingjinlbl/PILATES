import yaml
import docker
import os
import argparse
import logging
import sys
from pdb import set_trace as st

from pilates.activitysim import preprocessor as asim_pre
from pilates.activitysim import postprocessor as asim_post
from pilates.urbansim import preprocessor as usim_pre

logging.basicConfig(
    stream=sys.stdout, level=logging.INFO,
    format='%(name)s - %(levelname)s - %(message)s')


def formatted_print(string, width=50, fill_char='#'):
    print('\n')
    if len(string) + 2 > width:
        width = len(string) + 4
    print(fill_char * width)
    print('{:#^{width}}'.format(' ' + string + ' ', width=width))
    print(fill_char * width, '\n')


if __name__ == '__main__':

    # read settings from config file
    with open('settings.yaml') as file:
        settings = yaml.load(file, Loader=yaml.FullLoader)

    s3_io = settings['s3_io']
    land_use_image = settings['land_use_image']
    activity_demand_image = settings['activity_demand_image']
    region = settings['region']
    scenario = settings['scenario']
    start_year = settings['start_year']
    end_year = settings['end_year']
    land_use_freq = settings['land_use_freq']
    travel_model_freq = settings['travel_model_freq']
    household_sample_size = settings['household_sample_size']
    travel_model = settings['travel_model']
    
    if travel_model.lower() == 'manta':
        path_to_skims = settings['manta_path_to_skims']
        travel_model_image = settings['manta_travel_model_image']
    elif travel_model.lower() == 'beam':
        path_to_skims = settings['beam_path_to_skims']
        travel_model_image = settings['beam_travel_model_image']
    else:
        raise RuntimeError('Travel model can be either MANTA or BEAM')

    beam_local_config = settings['beam_local_config']
    beam_local_input_folder = settings['beam_local_input_folder']
    beam_local_output_folder = settings['beam_local_output_folder']
    manta_local_config_folder = settings['manta_local_config_folder']
    manta_local_config = settings['manta_local_config']
    manta_local_input_folder = settings['manta_local_input_folder']
    manta_local_output_folder = settings['manta_local_output_folder']
    skim_zone_source_id_col = settings['skim_zone_source_id_col']
    usim_client_data_folder = settings['usim_client_data_folder']
    usim_local_data_folder = settings['usim_local_data_folder']
    asim_bucket = settings['region_to_asim_bucket'][region]
    asim_subdir = settings['region_to_asim_subdir'][region]
    asim_workdir = os.path.join('/activitysim', asim_subdir)
    chunk_size = settings['chunk_size']
    num_processes = settings['num_processes']
    asim_local_input_folder = settings['asim_local_input_folder']
    asim_local_output_folder = settings['asim_local_output_folder']
    beam_subdir = settings['region_to_beam_subdir'][region]
    docker_stdout = settings['docker_stdout']
    pull_latest = settings['pull_latest']
    region_id = settings['region_to_region_id'][region]


    # parse args
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '-v', '--verbose', action='store_true', help='print docker stdout')
    parser.add_argument(
        '-p', '--pull_latest', action='store_true',
        help='pull latest docker images before running')
    parser.add_argument(
        "-h", "--household_sample_size", action="store",
        help="household sample size")
    args = parser.parse_args()
    if args.verbose:
        docker_stdout = True
    if args.pull_latest:
        pull_latest = True
    if args.household_sample_size:
        household_sample_size = args.household_sample_size

    # prep docker environment
    client = docker.from_env()
    if pull_latest:
        for image in [
                land_use_image, activity_demand_image, travel_model_image]:
            client.images.pull(image)

    # formattable runtime docker command strings
    formattable_usim_cmd = '-r {0} -i {1} -y {2} -f {3}'



    # 6. RUN MANTA
    path_to_manta_config = os.path.join(
        manta_local_config_folder, manta_local_config)

    # FIXME Adapt MANTA's Dockerfile
    print("Travel model image: {}".format(travel_model_image))
    st()
    client.containers.run(
        travel_model_image,
        volumes={
            os.path.abspath(manta_local_input_folder): {
                'bind': '/app/{0}'.format(manta_local_input_folder),
                'mode': 'rw'},
            os.path.abspath(manta_local_output_folder): {
                'bind': '/app/output',
                'mode': 'rw'}},
        command="qmake && cd LivingCity && make && ./LivingCity", # command="ls", 
        stdout=docker_stdout, stderr=True, detach=True, remove=True
    )
    st()

    # run the simulation flow
    for year in range(start_year, end_year, travel_model_freq):

        if land_use_freq > 0:

            forecast_year = year + travel_model_freq

            # 0. PREPARE URBANSIM DATA
            print_str = (
                "Preparing input data for land use development simulation.")
            formatted_print(print_str)

            usim_pre.add_skims_to_model_data(
                settings, region, skim_zone_source_id_col)

            # 1. RUN URBANSIM
            print_str = (
                "Simulating land use development from {0} "
                "to {1} with {2}.".format(
                    year, forecast_year, land_use_image.split('/')[1]))
            formatted_print(print_str)
            usim_cmd = formattable_usim_cmd.format(
                region_id, year, forecast_year, land_use_freq)
            usim = client.containers.run(
                land_use_image,
                volumes={
                    os.path.abspath(usim_local_data_folder): {
                        'bind': usim_client_data_folder,
                        'mode': 'rw'},
                },
                command=usim_cmd, stdout=docker_stdout,
                stderr=True, detach=True, remove=True)
            for log in usim.logs(
                    stream=True, stderr=True, stdout=docker_stdout):
                print(log)
        else:
            forecast_year = year

        # 3. PREPROCESS DATA FOR ACTIVITYSIM
        print_str = "Creating {0} input data from {1} outputs".format(
            activity_demand_image.split('/')[1], land_use_image.split('/')[1])
        formatted_print(print_str)
        asim_pre.create_skims_from_beam(asim_local_input_folder, settings)
        asim_pre.create_asim_data_from_h5(settings, forecast_year)

        # 4. RUN ACTIVITYSIM
        print_str = (
            "Generating activity plans for the year "
            "{0} with {1}".format(
                forecast_year, activity_demand_image))
        formatted_print(print_str)
        formattable_asim_cmd = '-y {0} -h {1} -n {2} -c {3}'
        asim = client.containers.run(
            activity_demand_image, working_dir=asim_workdir,
            volumes={
                os.path.abspath(settings['asim_local_input_folder']): {
                    'bind': os.path.join(asim_workdir, 'data'),
                    'mode': 'rw'},
                os.path.abspath(settings['asim_local_output_folder']): {
                    'bind': os.path.join(asim_workdir, 'output'),
                    'mode': 'rw'}
            },
            command=formattable_asim_cmd.format(
                forecast_year, household_sample_size,
                num_processes, chunk_size
            ),
            stdout=docker_stdout, stderr=True, detach=True, remove=True)
        for log in asim.logs(stream=True, stderr=True, stdout=docker_stdout):
            print(log)

        # 5. COPY ACTIVITYSIM OUTPUT --> URBANSIM INPUT
        print_str = (
            "Generating {0} BEAM and UrbanSim input data from "
            "{1} outputs".format(
                forecast_year, activity_demand_image))
        formatted_print(print_str)

        # If generating activities for the base year, don't overwrite
        # urbansim input data. This is usually only the case for warm
        # starts or debugging. Otherwise we want to set up urbansim for
        # the next simulation iteration
        if forecast_year != start_year:
            asim_post.create_next_iter_inputs(settings, forecast_year)

        if settings['travel_model'].lower() == 'beam':
            # # 6. RUN BEAM
            # path_to_beam_config = os.path.join(
            #     beam_local_input_folder, "input", beam_subdir,
            #     beam_local_config)
            # client.containers.run(
            #     travel_model_image,
            #     volumes={
            #         beam_local_input_folder: {
            #             'bind': '/app/{0}'.format(beam_local_input_folder),
            #             'mode': 'rw'},
            #         beam_local_output_folder: {
            #             'bind': '/app/output',
            #             'mode': 'rw'}},
            #     command="--config={0}".format(path_to_beam_config),
            #     stdout=docker_stdout, stderr=True, detach=True, remove=True
            # )

            # # # update path to skims
            # # new_skims_path = ????
            # # settings['path_to_skims'] = new_skims_path
            None
        elif settings['travel_model'].lower() == 'manta':
            None
        else:
            raise RuntimeError('Travel mode not supported, should be either MANTA or BEAM')
