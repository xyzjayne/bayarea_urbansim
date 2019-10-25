import orca
import pandas as pd
from urbansim.utils import misc
from validation import assert_series_equal


# TO ADD: Housing Unit imputation
# We want to match the target in baseyear_taz_controls.csv

# TO ADD: Nonresidential space imputation
# We want to match the target in baseyear_taz_controls.csv

# the way this works is there is an orca step to do jobs allocation, which
# reads base year totals and creates jobs and allocates them to buildings,
# and writes it back to the h5.  then the actual jobs table above just reads
# the auto-allocated version from the h5.  was hoping to just do allocation
# on the fly but it takes about 4 minutes so way to long to do on the fly


def allocate_jobs(baseyear_taz_controls, settings, buildings, parcels):
    # this does a new assignment from the controls to the buildings

    # first disaggregate the job totals
    sector_map = settings["naics_to_empsix"]
    jobs = []
    for taz, row in baseyear_taz_controls.local.iterrows():
        for sector_col, num in row.iteritems():

            # not a sector total
            if not sector_col.startswith("emp_sec"):
                continue

            # get integer sector id
            sector_id = int(''.join(c for c in sector_col if c.isdigit()))
            sector_name = sector_map[sector_id]

            jobs += [[sector_id, sector_name, taz, -1]] * int(num)

    df = pd.DataFrame(jobs, columns=[
        'sector_id', 'empsix', 'taz', 'building_id'])

    zone_id = misc.reindex(parcels.zone_id, buildings.parcel_id)

    # just do random assignment weighted by job spaces - we'll then
    # fill in the job_spaces if overfilled in the next step (code
    # has existed in urbansim for a while)
    for taz, cnt in df.groupby('taz').size().iteritems():

        potential_add_locations = buildings.non_residential_sqft[
            (zone_id == taz) &
            (buildings.non_residential_sqft > 0)]

        if len(potential_add_locations) == 0:
            # if no non-res buildings, put jobs in res buildings
            potential_add_locations = buildings.building_sqft[
                zone_id == taz]

        weights = potential_add_locations / potential_add_locations.sum()

        # print taz, len(potential_add_locations),\
        #     potential_add_locations.sum(), cnt

        buildings_ids = potential_add_locations.sample(
            cnt, replace=True, weights=weights)

        df["building_id"][df.taz == taz] = buildings_ids.index.values

    s = zone_id.loc[df.building_id].value_counts()
    # assert that we at least got the total employment right after assignment
    assert_series_equal(baseyear_taz_controls.emp_tot, s)

    return df


@orca.step()
def move_jobs_from_portola_to_san_mateo_county(parcels, buildings, jobs_df):
    # need to move jobs from portola valley to san mateo county
    NUM_IN_PORTOLA = 1500

    juris = misc.reindex(
        parcels.juris, misc.reindex(buildings.parcel_id, jobs_df.building_id))

    # find jobs in portols valley to move
    portola = jobs_df[juris == "Portola Valley"]
    move = portola.sample(len(portola) - NUM_IN_PORTOLA)

    # find places in san mateo to which to move them
    san_mateo = jobs_df[juris == "San Mateo County"]
    move_to = san_mateo.sample(len(move))

    jobs_df.loc[move.index, "building_id"] = move_to.building_id.values

    return jobs_df


@orca.step()
def preproc_jobs(store, baseyear_taz_controls, settings, parcels):
    buildings = store['buildings']

    jobs = allocate_jobs(baseyear_taz_controls, settings, buildings, parcels)
    jobs = move_jobs_from_portola_to_san_mateo_county(parcels, buildings, jobs)
    store['jobs_preproc'] = jobs


@orca.step()
def preproc_households(store):

    df = store['households']

    df['tenure'] = df.hownrent.map({1: 'own', 2: 'rent'})

    # need to keep track of base year income quartiles for use in the
    # transition model - even caching doesn't work because when you add
    # rows via the transitioning, you automatically clear the cache!
    # this is pretty nasty and unfortunate
    df["base_income_quartile"] = pd.Series(pd.qcut(df.income, 4, labels=False),
                                           index=df.index).add(1)
    df["base_income_octile"] = pd.Series(pd.qcut(df.income, 8, labels=False),
                                         index=df.index).add(1)

    # there are some overrides where we move households around in order
    # to match the city totals - in the future we will resynthesize and this
    # can go away - this csv is generated by scripts/match_city_totals.py
    overrides = pd.read_csv("data/household_building_id_overrides.csv",
                            index_col="household_id").building_id
    df.loc[overrides.index, "building_id"] = overrides.values

    # turns out we need 4 more households
    new_households = df.loc[[1132542, 1306618, 950630, 886585]].reset_index()
    # keep unique index
    new_households.index += pd.Series(df.index).max() + 1
    df = df.append(new_households)

    store['households_preproc'] = df


def assign_deed_restricted_units(df, parcels):

    df["deed_restricted_units"] = 0

    zone_ids = misc.reindex(parcels.zone_id, df.parcel_id).\
        reindex(df.index).fillna(-1)
    # sample deed restricted units to match current deed restricted unit
    # zone totals
    for taz, row in pd.read_csv('data/deed_restricted_zone_totals.csv',
                                index_col='taz_key').iterrows():

        cnt = row["units"]

        if cnt <= 0:
            continue

        potential_add_locations = df.residential_units[
            (zone_ids == taz) &
            (df.residential_units > 0)]

        assert len(potential_add_locations) > 0

        weights = potential_add_locations / potential_add_locations.sum()

        buildings_ids = potential_add_locations.sample(
            cnt, replace=True, weights=weights)

        units = pd.Series(buildings_ids.index.values).value_counts()
        df.loc[units.index, "deed_restricted_units"] += units.values

    print "Total deed restricted units after random selection: %d" % \
        df.deed_restricted_units.sum()

    df["deed_restricted_units"] = \
        df[["deed_restricted_units", "residential_units"]].min(axis=1)

    print "Total deed restricted units after truncating to res units: %d" % \
        df.deed_restricted_units.sum()

    return df


@orca.step()
def correct_baseyear_vacancies(buildings, parcels, jobs, store):
    # sonoma county has too much vacancy in the buildings so we're
    # going to lower it a bit to match job totals - I'm doing it here
    # as opposed to in datasources as it requires registered orca
    # variables

    '''
    These are the original vacancies
    Alameda          0.607865
    Contra Costa     0.464277
    Marin            0.326655
    Napa             0.427900
    San Francisco    0.714938
    San Mateo        0.285090
    Santa Clara      0.368031
    Solano           0.383663
    Sonoma           0.434263
    '''

    # get buildings by county
    buildings_county = misc.reindex(parcels.county, buildings.parcel_id)
    buildings_juris = misc.reindex(parcels.juris, buildings.parcel_id)

    # this is the maximum vacancy you can have any a building so it NOT the
    # same thing as setting the vacancy for the entire county
    SURPLUS_VACANCY_COUNTY = buildings_county.map({
       "Alameda": .42,
       "Contra Costa": .57,
       "Marin": .28,
       "Napa": .7,
       "San Francisco": .08,
       "San Mateo": .4,
       "Santa Clara": .32,
       "Solano": .53,
       "Sonoma": .4
    }).fillna(.2)

    SURPLUS_VACANCY_JURIS = buildings_juris.map({
       "Berkeley": .65,
       "Atherton": 0.05,
       "Belvedere": 0,
       "Corte Madera": 0,
       "Cupertino": .1,
       "Healdsburg": 0,
       "Larkspur": 0,
       "Los Altos Hills": 0,
       "Los Gatos": 0,
       "Monte Sereno": 0,
       "Piedmont": 0,
       "Portola Valley": 0,
       "Ross": 0,
       "San Anselmo": 0,
       "Saratoga": 0,
       "Woodside": 0,
       "Alameda": .2
    })

    SURPLUS_VACANCY = pd.DataFrame([
       SURPLUS_VACANCY_COUNTY, SURPLUS_VACANCY_JURIS]).min()

    # count of jobs by building
    job_counts_by_building = jobs.building_id.value_counts().\
        reindex(buildings.index).fillna(0)

    # with SURPLUS_VACANCY vacancy
    job_counts_by_building_surplus = \
        (job_counts_by_building * (SURPLUS_VACANCY+1)).astype('int')

    # min of job spaces and vacancy
    correct_job_spaces = pd.DataFrame([
        job_counts_by_building_surplus, buildings.job_spaces]).min()

    # convert back to non res sqft because job spaces is computed
    correct_non_res_sqft = correct_job_spaces * buildings.sqft_per_job

    buildings.update_col("non_residential_sqft", correct_non_res_sqft)

    jobs_county = misc.reindex(buildings_county, jobs.building_id)

    print "Vacancy rate by county:\n", \
        buildings.job_spaces.groupby(buildings_county).sum() / \
        jobs_county.value_counts() - 1.0

    jobs_juris = misc.reindex(buildings_juris, jobs.building_id)

    s = buildings.job_spaces.groupby(buildings_juris).sum() / \
        jobs_juris.value_counts() - 1.0
    print "Vacancy rate by juris:\n", s.to_string()

    return buildings


@orca.step()
def preproc_buildings(store, parcels, manual_edits):
    # start with buildings from urbansim_defaults
    df = store['buildings']

    # this is code from urbansim_defaults
    df["residential_units"] = pd.concat(
        [df.residential_units,
         store.households_preproc.building_id.value_counts()],
        axis=1).max(axis=1)

    # XXX need to make sure jobs don't exceed capacity

    # drop columns we don't needed
    df = df.drop(['development_type_id', 'improvement_value',
                  'sqft_per_unit', 'nonres_rent_per_sqft',
                  'res_price_per_sqft',
                  'redfin_home_type', 'costar_property_type',
                  'costar_rent'], axis=1)

    # apply manual edits
    edits = manual_edits.local
    edits = edits[edits.table == 'buildings']
    for index, row, col, val in \
            edits[["id", "attribute", "new_value"]].itertuples():
        df.set_value(row, col, val)

    df["residential_units"] = df.residential_units.fillna(0)

    # for some reason nonres can be more than total sqft
    df["building_sqft"] = pd.DataFrame({
        "one": df.building_sqft,
        "two": df.residential_sqft + df.non_residential_sqft}).max(axis=1)

    df["building_type"] = df.building_type_id.map({
      0: "O",
      1: "HS",
      2: "HT",
      3: "HM",
      4: "OF",
      5: "HO",
      6: "SC",
      7: "IL",
      8: "IW",
      9: "IH",
      10: "RS",
      11: "RB",
      12: "MR",
      13: "MT",
      14: "ME",
      15: "PA",
      16: "PA2"
    })

    del df["building_type_id"]  # we won't use building type ids anymore

    # keeps parking lots from getting redeveloped
    df["building_sqft"][df.building_type.isin(["PA", "PA2"])] = 0
    df["non_residential_sqft"][df.building_type.isin(["PA", "PA2"])] = 0

    # don't know what an other building type id, set to office
    df["building_type"] = df.building_type.replace("O", "OF")

    # set default redfin sale year to 2012
    df["redfin_sale_year"] = df.redfin_sale_year.fillna(2012)

    df["residential_price"] = 0.0
    df["non_residential_rent"] = 0.0

    df = assign_deed_restricted_units(df, parcels)

    store['buildings_preproc'] = df

    # this runs after the others because it needs access to orca-assigned
    # columns - in particular is needs access to the non-residential sqft and
    # job spaces columns
    orca.run(["correct_baseyear_vacancies"])


@orca.step()
def baseline_data_checks(store):
    # TODO

    # tests to make sure our baseline data edits worked as expected

    # spot check we match controls for jobs at the zonal level

    # spot check portola has 1500 jobs

    # check manual edits are applied

    # check deed restricted units match totals

    # check res units >= households

    # check job spaces >= jobs
    pass